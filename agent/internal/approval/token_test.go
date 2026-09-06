package approval

import (
	"errors"
	"testing"
)

// These two tokens were produced by the brain's Python implementation
// (coroner_brain.approval.sign) with the same inputs. If the Go side ever
// drifts from the Python side, this is the test that says so.
const (
	pythonApproved = "v1.16fff7d76c6b0066d037c8d24d1bea303c6342b755cdb48ec027019ad3002918"
	pythonEdited   = "v1.fba63c9eebec742eacaf4959de5e790d6adf65728fafa24126f322073808309e"
)

var secret = []byte("shared-secret")

func approved() Claims {
	return Claims{
		IncidentID:  "inc-abc",
		ContextHash: "0123456789abcdef",
		Decision:    "approved",
		Action:      "Raise the memory limit of hog to 256Mi",
		DecidedAt:   "2026-09-06T14:00:00+00:00",
	}
}

func TestMatchesThePythonImplementation(t *testing.T) {
	if got := Sign(secret, approved()); got != pythonApproved {
		t.Fatalf("Sign() = %s, want the Python token %s", got, pythonApproved)
	}
	edited := approved()
	edited.Decision = "edited"
	edited.Action = "Raise the memory limit of hog to 512Mi"
	if got := Sign(secret, edited); got != pythonEdited {
		t.Fatalf("Sign(edited) = %s, want the Python token %s", got, pythonEdited)
	}
	if err := Verify(secret, pythonApproved, approved()); err != nil {
		t.Fatalf("Verify() = %v, want nil", err)
	}
}

func TestEveryClaimIsBinding(t *testing.T) {
	cases := map[string]func(c *Claims){
		"incident":     func(c *Claims) { c.IncidentID = "inc-other" },
		"context hash": func(c *Claims) { c.ContextHash = "fedcba9876543210" },
		"action":       func(c *Claims) { c.Action = "delete the namespace" },
		"decided at":   func(c *Claims) { c.DecidedAt = "2026-09-07T14:00:00+00:00" },
		"decision":     func(c *Claims) { c.Decision = "edited" },
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			c := approved()
			mutate(&c)
			if err := Verify(secret, pythonApproved, c); !errors.Is(err, ErrMismatch) {
				t.Errorf("a changed %s must not verify, got %v", name, err)
			}
		})
	}
}

func TestOnlyAuthorisingDecisionsVerify(t *testing.T) {
	for _, decision := range []string{"rejected", "expired", ""} {
		c := approved()
		c.Decision = decision
		token := Sign(secret, c)
		if err := Verify(secret, token, c); err == nil {
			t.Errorf("decision %q must never verify, even with a matching signature", decision)
		}
	}
}

func TestWrongSecretAndMalformedTokens(t *testing.T) {
	if err := Verify([]byte("other"), pythonApproved, approved()); !errors.Is(err, ErrMismatch) {
		t.Errorf("wrong secret: got %v", err)
	}
	if err := Verify(nil, pythonApproved, approved()); err == nil {
		t.Error("an empty secret must refuse to verify anything")
	}
	for _, bad := range []string{"", "v0.abc", "16fff7d7", "v1"} {
		if err := Verify(secret, bad, approved()); !errors.Is(err, ErrMalformed) {
			t.Errorf("token %q: got %v, want ErrMalformed", bad, err)
		}
	}
}
