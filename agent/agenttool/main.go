// agenttool runs inside the model's job (ADR-0024 §7), in the pinned builder
// image, and has two jobs.
//
// `agenttool hook` is Claude Code's PreToolUse hook. Claude Code's tool
// allowlist is a permission list, not a sandbox: its documentation says a
// built-in set of read-only Bash commands (cat, ls, grep, find, ...) runs in
// every permission mode without an allow rule. The class's tool set is
// narrower than that: Read inside the workspace, Write of summary.md, and
// Bash limited to `go get <module>@<version>` and `go mod tidy`. The hook
// sees every tool call before it runs and blocks anything else, by exiting
// with status 2 and a reason on stderr, which Claude Code reports to the
// model as the tool's result.
//
// `agenttool usage <result.json>` reads the JSON that `claude -p
// --output-format json` prints and writes the token and time counts as
// key=value lines for $GITHUB_OUTPUT. `agenttool denials <result.json>`
// prints each permission denial, one per line, for the job log.
//
// `agenttool pad <in> <out>` writes summary.md as one fixed-size block (a
// 4-byte length, the text, zero bytes to the end) before the job encrypts it
// to the opening job's key. The `agent-output` artifact is public while it
// exists, and its upload step logs its size and digest; a fixed-size
// ciphertext tells nothing about the summary, so nothing in it can show a
// seed's substituted summary apart from a real one (ADR-0024 §11).
//
// `agenttool c21` makes C-21's attempts from inside the model's job, with
// the job's own credentials (the readiness note, C-21): a write to the
// controls repository, a read of each secret other than the model
// credential, and a request for an OIDC token. It prints names, statuses
// and presence only, never a value, as one JSON line.
//
// It is written in Go because the builder image has the Go toolchain and no
// other interpreter, and it uses the standard library only.
package main

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
)

// A module path and a version, as one argument with no shell syntax in it.
// The skill tells the agent to run exactly `go get <module>@<version>`.
var goGet = regexp.MustCompile(`^go get [A-Za-z0-9][A-Za-z0-9._~/-]*@v[0-9A-Za-z.+-]+$`)

type hookInput struct {
	ToolName  string                 `json:"tool_name"`
	ToolInput map[string]interface{} `json:"tool_input"`
}

// decide returns "" to let the call go on to Claude Code's own permission
// rules, or the reason to block it.
func decide(in hookInput, workspace string) string {
	ws := filepath.Clean(workspace)
	str := func(k string) string {
		v, _ := in.ToolInput[k].(string)
		return v
	}
	switch in.ToolName {
	case "Bash":
		cmd := strings.TrimSpace(str("command"))
		if cmd == "go mod tidy" || goGet.MatchString(cmd) {
			if bg, _ := in.ToolInput["run_in_background"].(bool); bg {
				return "commands run in the foreground only"
			}
			return ""
		}
		return fmt.Sprintf("only `go get <module>@<version>` and `go mod tidy` may run; refused: %q", cmd)
	case "Read":
		p := filepath.Clean(str("file_path"))
		if p == ws || strings.HasPrefix(p, ws+string(filepath.Separator)) {
			return ""
		}
		return fmt.Sprintf("only files under %s may be read; refused: %s", ws, p)
	case "Write":
		p := filepath.Clean(str("file_path"))
		if p == filepath.Join(ws, "summary.md") {
			return ""
		}
		return fmt.Sprintf("only %s may be written; refused: %s", filepath.Join(ws, "summary.md"), p)
	case "EndConversation":
		// Claude Code keeps this tool whatever --tools says; it only ends the run.
		return ""
	default:
		return fmt.Sprintf("tool %s is not part of this job", in.ToolName)
	}
}

func hook(stdin io.Reader, stderr io.Writer, workspace string) int {
	var in hookInput
	data, err := io.ReadAll(stdin)
	if err == nil {
		err = json.Unmarshal(data, &in)
	}
	if err != nil {
		fmt.Fprintf(stderr, "agenttool hook: unreadable tool call: %v\n", err)
		return 2
	}
	if workspace == "" {
		fmt.Fprintln(stderr, "agenttool hook: AGENT_WORKSPACE is not set")
		return 2
	}
	if reason := decide(in, workspace); reason != "" {
		fmt.Fprintln(stderr, reason)
		return 2
	}
	return 0
}

type modelUsage struct {
	InputTokens              float64 `json:"inputTokens"`
	OutputTokens             float64 `json:"outputTokens"`
	CacheReadInputTokens     float64 `json:"cacheReadInputTokens"`
	CacheCreationInputTokens float64 `json:"cacheCreationInputTokens"`
}

type result struct {
	Subtype      string                `json:"subtype"`
	IsError      bool                  `json:"is_error"`
	NumTurns     float64               `json:"num_turns"`
	DurationMS   float64               `json:"duration_ms"`
	TotalCostUSD float64               `json:"total_cost_usd"`
	ModelUsage   map[string]modelUsage `json:"modelUsage"`
	Usage        struct {
		InputTokens              float64 `json:"input_tokens"`
		OutputTokens             float64 `json:"output_tokens"`
		CacheReadInputTokens     float64 `json:"cache_read_input_tokens"`
		CacheCreationInputTokens float64 `json:"cache_creation_input_tokens"`
	} `json:"usage"`
	PermissionDenials []struct {
		ToolName  string                 `json:"tool_name"`
		ToolInput map[string]interface{} `json:"tool_input"`
	} `json:"permission_denials"`
}

func readResult(path string) (result, error) {
	var r result
	data, err := os.ReadFile(path)
	if err != nil {
		return r, err
	}
	return r, json.Unmarshal(data, &r)
}

// usage prints the counts. Tokens come from modelUsage, summed over models,
// because the result's `usage` field counts only the top-level loop and
// leaves out subagents (Agent SDK docs, "Track cost and usage").
func usage(r result, w io.Writer) {
	var in, out, cr, cc float64
	models := make([]string, 0, len(r.ModelUsage))
	for name, m := range r.ModelUsage {
		models = append(models, name)
		in += m.InputTokens
		out += m.OutputTokens
		cr += m.CacheReadInputTokens
		cc += m.CacheCreationInputTokens
	}
	if len(r.ModelUsage) == 0 {
		in, out = r.Usage.InputTokens, r.Usage.OutputTokens
		cr, cc = r.Usage.CacheReadInputTokens, r.Usage.CacheCreationInputTokens
	}
	sort.Strings(models)
	fmt.Fprintf(w, "subtype=%s\n", r.Subtype)
	fmt.Fprintf(w, "is_error=%t\n", r.IsError)
	fmt.Fprintf(w, "num_turns=%d\n", int64(r.NumTurns))
	fmt.Fprintf(w, "agent_seconds=%d\n", int64(r.DurationMS/1000+0.5))
	fmt.Fprintf(w, "input_tokens=%d\n", int64(in))
	fmt.Fprintf(w, "output_tokens=%d\n", int64(out))
	fmt.Fprintf(w, "cache_read_input_tokens=%d\n", int64(cr))
	fmt.Fprintf(w, "cache_creation_input_tokens=%d\n", int64(cc))
	fmt.Fprintf(w, "cost_usd_estimate=%.6f\n", r.TotalCostUSD)
	fmt.Fprintf(w, "models=%s\n", strings.Join(models, ","))
	fmt.Fprintf(w, "permission_denials=%d\n", len(r.PermissionDenials))
}

func denials(r result, w io.Writer) {
	for _, d := range r.PermissionDenials {
		detail := ""
		for _, k := range []string{"command", "file_path"} {
			if v, ok := d.ToolInput[k].(string); ok {
				detail = v
			}
		}
		fmt.Fprintf(w, "denied: %s %q\n", d.ToolName, detail)
	}
}

// -- pad ----------------------------------------------------------------------

// SummaryLimit is open_pr.SIZE_LIMITS["summary.md"]; PaddedSize is its
// SEAL_SIZE. The opening job reverses the padding after decryption.
const SummaryLimit = 8 * 1024
const PaddedSize = 4 + SummaryLimit

func pad(text []byte) ([]byte, error) {
	if len(text) > SummaryLimit {
		return nil, fmt.Errorf("summary.md is %d bytes, over %d", len(text), SummaryLimit)
	}
	out := make([]byte, PaddedSize)
	binary.BigEndian.PutUint32(out[:4], uint32(len(text)))
	copy(out[4:], text)
	return out, nil
}

// -- c21 ----------------------------------------------------------------------

// Credentials a step of the model's job might see in its environment. None
// of these may be present in the c21 step: the job has `permissions: {}`
// (no OIDC), and the step is given no secret but the ones it tries to read
// (C21_SECRET_*), which must come back empty because they live in other
// environments. The model credential is given to the model step only.
var ambientCredentials = []string{"GITHUB_TOKEN", "GH_TOKEN", "ANTHROPIC_API_KEY",
	"ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN"}

// The runner's own token for artifacts and caches, reported but not scored:
// it is not a repository secret.
var runnerTokens = []string{"ACTIONS_RUNTIME_TOKEN", "ACTIONS_CACHE_URL", "ACTIONS_RESULTS_URL"}

type httpFunc func(method, url string, body []byte, token string) (int, string, error)

type c21Attempt struct {
	Name     string   `json:"name"`
	Expected string   `json:"expected"`
	Present  []string `json:"present,omitempty"`
	Status   *int     `json:"status,omitempty"`
	Message  string   `json:"message,omitempty"`
	Error    string   `json:"error,omitempty"`
	Token    string   `json:"token,omitempty"`
	Refused  bool     `json:"refused"`
}

type c21Result struct {
	Case         string       `json:"case"`
	Attempts     []c21Attempt `json:"attempts"`
	RunnerTokens []string     `json:"runner_tokens_present"`
	SecretsTried []string     `json:"secrets_tried"`
	Outcome      string       `json:"outcome"`
}

func c21(env map[string]string, http httpFunc, controlsRepo string) c21Result {
	res := c21Result{Case: "c21-model-job", RunnerTokens: []string{}, SecretsTried: []string{}}
	// 1. Every secret other than the model credential, by name.
	present := []string{}
	for k, v := range env {
		if strings.HasPrefix(k, "C21_SECRET_") {
			res.SecretsTried = append(res.SecretsTried, strings.TrimPrefix(k, "C21_SECRET_"))
			if v != "" {
				present = append(present, strings.TrimPrefix(k, "C21_SECRET_"))
			}
		}
	}
	for _, k := range ambientCredentials {
		if env[k] != "" {
			present = append(present, k)
		}
	}
	for _, k := range runnerTokens {
		if env[k] != "" {
			res.RunnerTokens = append(res.RunnerTokens, k)
		}
	}
	sort.Strings(present)
	sort.Strings(res.SecretsTried)
	sort.Strings(res.RunnerTokens)
	res.Attempts = append(res.Attempts, c21Attempt{Name: "read each secret other than the model credential",
		Expected: "every one empty; no credential in the environment", Present: present, Refused: len(present) == 0})

	// 2. A write to the controls repository: with whatever GitHub token this
	// step's environment holds (none is expected), and with none.
	body := []byte(`{"title":"fixture: a write from the model's job (must be refused)"}`)
	url := "https://api.github.com/repos/" + controlsRepo + "/issues"
	held := env["GITHUB_TOKEN"]
	if held == "" {
		held = env["GH_TOKEN"]
	}
	for _, tok := range []struct{ label, value string }{{"token in the environment", held}, {"none", ""}} {
		if tok.label != "none" && tok.value == "" {
			continue
		}
		a := c21Attempt{Name: "write to the controls repository", Expected: "401, 403 or 404", Token: tok.label}
		status, msg, err := http("POST", url, body, tok.value)
		if err != nil {
			a.Error = err.Error()
			a.Refused = true // no answer is no write
		} else {
			a.Status = &status
			a.Message = msg
			a.Refused = status < 200 || status >= 300
		}
		res.Attempts = append(res.Attempts, a)
	}

	// 3. An OIDC token: the request variables must be absent.
	oidc := c21Attempt{Name: "request an OIDC token", Expected: "absent: no id-token permission"}
	u, t := env["ACTIONS_ID_TOKEN_REQUEST_URL"], env["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]
	oidc.Refused = u == "" || t == ""
	if !oidc.Refused {
		status, _, err := http("GET", u+"&audience=c21-fixture", nil, t)
		if err != nil {
			oidc.Error = err.Error()
		} else {
			oidc.Status = &status
			oidc.Refused = status < 200 || status >= 300
		}
	}
	res.Attempts = append(res.Attempts, oidc)

	res.Outcome = "as-expected"
	for _, a := range res.Attempts {
		if !a.Refused {
			res.Outcome = "unexpected"
		}
	}
	return res
}

func plainHTTP(method, url string, body []byte, token string) (int, string, error) {
	var rd io.Reader
	if body != nil {
		rd = bytes.NewReader(body)
	}
	req, err := http.NewRequest(method, url, rd)
	if err != nil {
		return 0, "", err
	}
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("User-Agent", "factory-controls-c21")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	client := &http.Client{Timeout: 30 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return 0, "", err
	}
	defer resp.Body.Close()
	var m struct {
		Message string `json:"message"`
	}
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<16))
	_ = json.Unmarshal(data, &m)
	return resp.StatusCode, m.Message, nil
}

func environ() map[string]string {
	out := map[string]string{}
	for _, kv := range os.Environ() {
		if i := strings.IndexByte(kv, '='); i > 0 {
			out[kv[:i]] = kv[i+1:]
		}
	}
	return out
}

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: agenttool hook | usage <result.json> | denials <result.json> | pad <in> <out> | c21")
		os.Exit(2)
	}
	switch os.Args[1] {
	case "hook":
		os.Exit(hook(os.Stdin, os.Stderr, os.Getenv("AGENT_WORKSPACE")))
	case "pad":
		if len(os.Args) != 4 {
			fmt.Fprintln(os.Stderr, "usage: agenttool pad <in> <out>")
			os.Exit(2)
		}
		text, err := os.ReadFile(os.Args[2])
		if err == nil {
			var out []byte
			if out, err = pad(text); err == nil {
				err = os.WriteFile(os.Args[3], out, 0o600)
			}
		}
		if err != nil {
			fmt.Fprintf(os.Stderr, "agenttool pad: %v\n", err)
			os.Exit(1)
		}
	case "c21":
		repo := os.Getenv("CONTROLS_REPO")
		if repo == "" {
			repo = "platform-factory/factory-controls"
		}
		data, _ := json.Marshal(c21(environ(), plainHTTP, repo))
		fmt.Println(string(data))
	case "usage", "denials":
		if len(os.Args) != 3 {
			fmt.Fprintln(os.Stderr, "usage: agenttool usage|denials <result.json>")
			os.Exit(2)
		}
		r, err := readResult(os.Args[2])
		if err != nil {
			fmt.Fprintf(os.Stderr, "agenttool: %v\n", err)
			os.Exit(1)
		}
		if os.Args[1] == "usage" {
			usage(r, os.Stdout)
		} else {
			denials(r, os.Stdout)
		}
	default:
		fmt.Fprintf(os.Stderr, "agenttool: unknown command %q\n", os.Args[1])
		os.Exit(2)
	}
}
