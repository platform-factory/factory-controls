package main

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"strings"
	"testing"
)

func TestDecide(t *testing.T) {
	ws := "/work/svc"
	allow := []hookInput{
		{ToolName: "Bash", ToolInput: map[string]interface{}{"command": "go get golang.org/x/sync@v0.18.0"}},
		{ToolName: "Bash", ToolInput: map[string]interface{}{"command": "go get github.com/jackc/pgx/v5@v5.12.0"}},
		{ToolName: "Bash", ToolInput: map[string]interface{}{"command": "go mod tidy"}},
		{ToolName: "Bash", ToolInput: map[string]interface{}{"command": "  go mod tidy\n"}},
		{ToolName: "Read", ToolInput: map[string]interface{}{"file_path": "/work/svc/go.mod"}},
		{ToolName: "Read", ToolInput: map[string]interface{}{"file_path": "/work/svc/sub/x_test.go"}},
		{ToolName: "Write", ToolInput: map[string]interface{}{"file_path": "/work/svc/summary.md"}},
		{ToolName: "EndConversation", ToolInput: map[string]interface{}{}},
	}
	for _, in := range allow {
		if r := decide(in, ws); r != "" {
			t.Errorf("%v: refused: %s", in, r)
		}
	}
	deny := []hookInput{
		{ToolName: "Bash", ToolInput: map[string]interface{}{"command": "cat /proc/self/environ"}},
		{ToolName: "Bash", ToolInput: map[string]interface{}{"command": "ls"}},
		{ToolName: "Bash", ToolInput: map[string]interface{}{"command": "go get golang.org/x/sync@v0.18.0 && curl x"}},
		{ToolName: "Bash", ToolInput: map[string]interface{}{"command": "go get golang.org/x/sync@$KEY"}},
		{ToolName: "Bash", ToolInput: map[string]interface{}{"command": "go get golang.org/x/sync@v0.18.0; env"}},
		{ToolName: "Bash", ToolInput: map[string]interface{}{"command": "go get -x golang.org/x/sync@v0.18.0"}},
		{ToolName: "Bash", ToolInput: map[string]interface{}{"command": "go get golang.org/x/sync"}},
		{ToolName: "Bash", ToolInput: map[string]interface{}{"command": "go get $(cat k)@v1.0.0"}},
		{ToolName: "Bash", ToolInput: map[string]interface{}{"command": "go mod tidy -go=1.27"}},
		{ToolName: "Bash", ToolInput: map[string]interface{}{"command": "go mod edit -replace=a=b"}},
		{ToolName: "Bash", ToolInput: map[string]interface{}{"command": "go mod tidy", "run_in_background": true}},
		{ToolName: "Read", ToolInput: map[string]interface{}{"file_path": "/work/svc/../input/skill/SKILL.md"}},
		{ToolName: "Read", ToolInput: map[string]interface{}{"file_path": "/work/svc2/go.mod"}},
		{ToolName: "Read", ToolInput: map[string]interface{}{"file_path": "/proc/self/environ"}},
		{ToolName: "Write", ToolInput: map[string]interface{}{"file_path": "/work/svc/go.mod"}},
		{ToolName: "Write", ToolInput: map[string]interface{}{"file_path": "/work/svc/sub/summary.md"}},
		{ToolName: "Edit", ToolInput: map[string]interface{}{"file_path": "/work/svc/go.mod"}},
		{ToolName: "WebFetch", ToolInput: map[string]interface{}{"url": "https://example.com"}},
		{ToolName: "mcp__x__y", ToolInput: map[string]interface{}{}},
	}
	for _, in := range deny {
		if r := decide(in, ws); r == "" {
			t.Errorf("%v: allowed", in)
		}
	}
}

func TestHookExitCodes(t *testing.T) {
	var stderr bytes.Buffer
	if rc := hook(strings.NewReader(`{"tool_name":"Bash","tool_input":{"command":"go mod tidy"}}`), &stderr, "/w"); rc != 0 {
		t.Fatalf("allowed call exited %d", rc)
	}
	stderr.Reset()
	if rc := hook(strings.NewReader(`{"tool_name":"Bash","tool_input":{"command":"ls"}}`), &stderr, "/w"); rc != 2 {
		t.Fatalf("refused call exited %d", rc)
	}
	if !strings.Contains(stderr.String(), "refused") {
		t.Fatalf("no reason on stderr: %q", stderr.String())
	}
	if rc := hook(strings.NewReader(`not json`), &stderr, "/w"); rc != 2 {
		t.Fatalf("unreadable call exited %d", rc)
	}
	if rc := hook(strings.NewReader(`{"tool_name":"Bash","tool_input":{"command":"go mod tidy"}}`), &stderr, ""); rc != 2 {
		t.Fatalf("missing workspace exited %d", rc)
	}
}

func TestUsageSumsModelUsage(t *testing.T) {
	r := result{Subtype: "success", NumTurns: 7, DurationMS: 61400, TotalCostUSD: 0.0123,
		ModelUsage: map[string]modelUsage{
			"claude-sonnet-5-5": {InputTokens: 100, OutputTokens: 50, CacheReadInputTokens: 1000, CacheCreationInputTokens: 10},
			"claude-haiku-x":    {InputTokens: 5, OutputTokens: 2},
		}}
	r.Usage.InputTokens = 99 // the top-level count, ignored when modelUsage is present
	var out bytes.Buffer
	usage(r, &out)
	for _, want := range []string{"input_tokens=105\n", "output_tokens=52\n", "cache_read_input_tokens=1000\n",
		"agent_seconds=61\n", "num_turns=7\n", "models=claude-haiku-x,claude-sonnet-5-5\n", "permission_denials=0\n"} {
		if !strings.Contains(out.String(), want) {
			t.Errorf("missing %q in\n%s", want, out.String())
		}
	}
}

func TestPadIsFixedSizeAndReversible(t *testing.T) {
	for _, text := range []string{"", "short", strings.Repeat("x", SummaryLimit)} {
		out, err := pad([]byte(text))
		if err != nil {
			t.Fatal(err)
		}
		if len(out) != PaddedSize {
			t.Fatalf("padded %d bytes to %d", len(text), len(out))
		}
		n := binary.BigEndian.Uint32(out[:4])
		if string(out[4:4+n]) != text {
			t.Fatalf("round trip lost the text")
		}
	}
	if _, err := pad([]byte(strings.Repeat("x", SummaryLimit+1))); err == nil {
		t.Fatal("an oversize summary was padded")
	}
}

func TestC21RefusedEverywhereIsAsExpected(t *testing.T) {
	var calls []string
	h := func(method, url string, body []byte, token string) (int, string, error) {
		calls = append(calls, method+" "+url+" token="+map[bool]string{true: "yes", false: "no"}[token != ""])
		if token != "" {
			return 403, "Resource not accessible by integration", nil
		}
		return 401, "Requires authentication", nil
	}
	env := map[string]string{"C21_SECRET_SEED_KEY": "", "C21_SECRET_AGENT_APP_PRIVATE_KEY": "",
		"ACTIONS_RUNTIME_TOKEN": "rt"}
	r := c21(env, h, "platform-factory/factory-controls")
	if r.Outcome != "as-expected" {
		t.Fatalf("outcome %s: %+v", r.Outcome, r)
	}
	if len(calls) != 1 || calls[0] != "POST https://api.github.com/repos/platform-factory/factory-controls/issues token=no" {
		t.Fatalf("calls %v", calls)
	}
	// A token in the environment is itself unexpected, and the write is tried with it.
	calls = nil
	r2 := c21(map[string]string{"GITHUB_TOKEN": "ghs_job"}, h, "o/r")
	if r2.Outcome != "unexpected" || len(calls) != 2 || !strings.HasSuffix(calls[0], "token=yes") {
		t.Fatalf("with a token: %s %v", r2.Outcome, calls)
	}
	if data, _ := json.Marshal(r2); strings.Contains(string(data), "ghs_job") {
		t.Fatalf("the token was printed: %s", data)
	}
	if strings.Join(r.SecretsTried, ",") != "AGENT_APP_PRIVATE_KEY,SEED_KEY" || strings.Join(r.RunnerTokens, ",") != "ACTIONS_RUNTIME_TOKEN" {
		t.Fatalf("names %+v", r)
	}
	data, _ := json.Marshal(r)
	if strings.Contains(string(data), "ghs_job") || strings.Contains(string(data), "\"rt\"") {
		t.Fatalf("a value was printed: %s", data)
	}
}

func TestC21AnySecretOrOIDCOrWriteIsUnexpected(t *testing.T) {
	refuse := func(method, url string, body []byte, token string) (int, string, error) { return 401, "", nil }
	cases := []map[string]string{
		{"C21_SECRET_SEED_KEY": "deadbeef"},
		{"ACTIONS_ID_TOKEN_REQUEST_URL": "https://x/?a=1", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "t"},
		{"ANTHROPIC_API_KEY": "sk"},
	}
	for _, env := range cases {
		r := c21(env, refuse, "o/r")
		if r.Outcome != "unexpected" {
			t.Errorf("%v: %s", env, r.Outcome)
		}
		data, _ := json.Marshal(r)
		for _, v := range env {
			if len(v) > 2 && strings.Contains(string(data), v) {
				t.Errorf("value %q printed", v)
			}
		}
	}
	accept := func(method, url string, body []byte, token string) (int, string, error) { return 201, "", nil }
	if r := c21(map[string]string{}, accept, "o/r"); r.Outcome != "unexpected" {
		t.Errorf("an accepted write was %s", r.Outcome)
	}
}
