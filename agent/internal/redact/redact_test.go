package redact

import (
	"strings"
	"testing"
)

func TestRedactsCredentials(t *testing.T) {
	cases := []struct {
		name     string
		in       string
		wantKind string
		gone     string
	}{
		{
			name:     "password inside a connection string",
			in:       "dial postgres://orders:hunter2@db.internal:5432/orders failed",
			wantKind: "connection-string-password",
			gone:     "hunter2",
		},
		{
			name:     "bearer token",
			in:       "Authorization: Bearer abcdefghijklmnop1234567890",
			wantKind: "bearer-token",
			gone:     "abcdefghijklmnop1234567890",
		},
		{
			name:     "aws access key id",
			in:       "using key AKIAIOSFODNN7EXAMPLE for upload",
			wantKind: "aws-access-key-id",
			gone:     "AKIAIOSFODNN7EXAMPLE",
		},
		{
			name:     "assigned secret",
			in:       "starting with API_KEY=s3cr3tvalue0987",
			wantKind: "assigned-secret",
			gone:     "s3cr3tvalue0987",
		},
		{
			name:     "json web token",
			in:       "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
			wantKind: "jwt",
			gone:     "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
		},
		{
			name:     "private key block",
			in:       "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAKj3\n-----END RSA PRIVATE KEY-----",
			wantKind: "private-key",
			gone:     "MIIBOgIBAAJBAKj3",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := Text(tc.in)
			if strings.Contains(got.Text, tc.gone) {
				t.Errorf("secret survived redaction: %q still in %q", tc.gone, got.Text)
			}
			if got.Count == 0 {
				t.Error("Count = 0, want at least one redaction")
			}
			if !contains(got.Kinds, tc.wantKind) {
				t.Errorf("Kinds = %v, want it to include %q", got.Kinds, tc.wantKind)
			}
		})
	}
}

// The causal line in the recorded CrashLoopBackOff fixture must survive intact.
// Over-redaction here would destroy the only evidence that names the failure,
// which is a worse outcome than the marginal secrecy gained.
func TestPreservesEvidenceThatIsNotSecret(t *testing.T) {
	cases := []struct {
		name string
		in   string
	}{
		{
			name: "connection string with a user but no password",
			in:   "[config]  DATABASE_URL=postgres://orders@db.internal:5432/orders",
		},
		{
			name: "the recorded fatal line",
			in:   "[error]   dial tcp 10.96.31.14:5432: connect: connection refused",
		},
		{
			name: "an ordinary image reference",
			in:   `Failed to pull image "ghcr.io/saadhtiwana/coroner-does-not-exist:v0.0.0": 403 Forbidden`,
		},
		{
			name: "a host:port pair",
			in:   "listening on 0.0.0.0:8080",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := Text(tc.in)
			if got.Count != 0 {
				t.Errorf("redacted %d item(s) from non-secret text\n  in:  %q\n  out: %q\n  kinds: %v",
					got.Count, tc.in, got.Text, got.Kinds)
			}
			if got.Text != tc.in {
				t.Errorf("text was modified\n  in:  %q\n  out: %q", tc.in, got.Text)
			}
		})
	}
}

func TestEmptyInput(t *testing.T) {
	got := Text("")
	if got.Count != 0 || got.Text != "" || len(got.Kinds) != 0 {
		t.Errorf("Text(\"\") = %+v, want zero value", got)
	}
}

func TestMergeAccumulates(t *testing.T) {
	a := Text("Bearer abcdefghijklmnop1234567890")
	b := Text("key AKIAIOSFODNN7EXAMPLE")
	a.Merge(b)
	if a.Count != 2 {
		t.Errorf("Count = %d, want 2", a.Count)
	}
	if !contains(a.Kinds, "bearer-token") || !contains(a.Kinds, "aws-access-key-id") {
		t.Errorf("Kinds = %v, want both kinds", a.Kinds)
	}
}

func contains(hay []string, needle string) bool {
	for _, h := range hay {
		if h == needle {
			return true
		}
	}
	return false
}
