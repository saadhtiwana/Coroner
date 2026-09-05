// Package redact removes credential-shaped text from collected evidence.
//
// It runs in the agent, before anything leaves the cluster, because the brain
// is a separate process that talks to a third-party model API. Redaction
// reports what it withheld so the brain can distinguish evidence that was
// withheld from evidence that was never there.
//
// The patterns are deliberately conservative. Over-redaction destroys the
// causal line that a CrashLoopBackOff diagnosis depends on, and a connection
// string without a password is evidence rather than a secret, so it is kept.
package redact

import (
	"regexp"
	"sort"
)

// Rule is a named redaction pattern.
type Rule struct {
	Kind    string
	Pattern *regexp.Regexp
	// Replacement receives the whole match and returns what replaces it.
	Replacement func(match string) string
}

// Result reports redacted text alongside what was removed.
type Result struct {
	Text  string
	Count int
	Kinds []string
}

func placeholder(kind string) func(string) string {
	return func(string) string { return "[REDACTED:" + kind + "]" }
}

// rules are applied in order. More specific patterns run first so a broad rule
// cannot swallow a match a precise rule would have labelled better.
var rules = []Rule{
	{
		Kind:        "private-key",
		Pattern:     regexp.MustCompile(`(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----`),
		Replacement: placeholder("private-key"),
	},
	{
		Kind:        "jwt",
		Pattern:     regexp.MustCompile(`eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}`),
		Replacement: placeholder("jwt"),
	},
	{
		Kind:        "bearer-token",
		Pattern:     regexp.MustCompile(`(?i)\b(bearer|token)\s+[A-Za-z0-9\-._~+/]{12,}={0,2}`),
		Replacement: placeholder("bearer-token"),
	},
	{
		Kind:        "aws-access-key-id",
		Pattern:     regexp.MustCompile(`\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}\b`),
		Replacement: placeholder("aws-access-key-id"),
	},
	{
		// Only when credentials are actually embedded. A URL carrying a
		// username but no password stays intact: it names the dependency that
		// failed, which is the single most useful fact in a crash log.
		Kind:    "connection-string-password",
		Pattern: regexp.MustCompile(`(?i)\b([a-z][a-z0-9+.-]*://)([^:@/\s]+):([^@/\s]+)@`),
		Replacement: func(m string) string {
			sub := connStringRe.FindStringSubmatch(m)
			if len(sub) != 4 {
				return "[REDACTED:connection-string-password]"
			}
			return sub[1] + sub[2] + ":[REDACTED:password]@"
		},
	},
	{
		Kind:        "assigned-secret",
		Pattern:     regexp.MustCompile(`(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret)\b\s*[:=]\s*"?([^\s"',;]{4,})"?`),
		Replacement: placeholder("assigned-secret"),
	},
}

var connStringRe = regexp.MustCompile(`(?i)\b([a-z][a-z0-9+.-]*://)([^:@/\s]+):([^@/\s]+)@`)

// Text applies every rule and reports what was removed.
func Text(in string) Result {
	if in == "" {
		return Result{}
	}
	out := in
	seen := map[string]bool{}
	total := 0

	for _, r := range rules {
		matches := r.Pattern.FindAllString(out, -1)
		if len(matches) == 0 {
			continue
		}
		total += len(matches)
		seen[r.Kind] = true
		out = r.Pattern.ReplaceAllStringFunc(out, r.Replacement)
	}

	kinds := make([]string, 0, len(seen))
	for k := range seen {
		kinds = append(kinds, k)
	}
	sort.Strings(kinds)

	return Result{Text: out, Count: total, Kinds: kinds}
}

// Merge folds one result's counters into another, for accumulating across the
// several fields a contract redacts.
func (r *Result) Merge(other Result) {
	r.Count += other.Count
	seen := map[string]bool{}
	for _, k := range r.Kinds {
		seen[k] = true
	}
	for _, k := range other.Kinds {
		if !seen[k] {
			seen[k] = true
			r.Kinds = append(r.Kinds, k)
		}
	}
	sort.Strings(r.Kinds)
}
