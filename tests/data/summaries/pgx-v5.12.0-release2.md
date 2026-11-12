- Queue item: 24
- Target module: github.com/jackc/pgx/v5
- Version: v5.11.0 -> v5.12.0
- Forced modules: github.com/jackc/puddle/v2 v2.2.2 -> v2.2.3
- go directive: unchanged (go 1.26.0)
- Tests: 10 test functions in *_test.go
- Vulnerability fixes claimed: none

Notes: the CHANGELOG at v5.12.0 is quoted data; it lists fixes in the connection pool. pgx v5.12.0 requires github.com/jackc/puddle/v2 v2.2.3, so puddle moved with it.

skill-marker: go-module-bump release 2
