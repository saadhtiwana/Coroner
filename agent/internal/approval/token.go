// Package approval verifies the token the brain mints when a human approves
// a diagnosis.
//
// The agent is the only component holding cluster credentials, and it will
// not execute an action whose approval token it cannot verify. The token is
// an HMAC over the incident id, the context hash of the evidence the
// diagnosis rested on, the decision, the exact action text, and the decision
// time, keyed with a secret shared with the brain. It is computed here
// exactly as brain/src/coroner_brain/approval.py computes it, and the test
// vectors in token_test.go were produced by that Python code.
package approval

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"strings"
)

// Version is the token format prefix. Both sides check it.
const Version = "v1"

// Claims are the fields an approval binds. Changing any one of them, the
// action text included, invalidates the token.
type Claims struct {
	IncidentID  string
	ContextHash string
	Decision    string
	Action      string
	DecidedAt   string
}

// ErrMalformed means the token is not in the expected shape at all.
var ErrMalformed = errors.New("approval token is malformed")

// ErrMismatch means the token does not verify for these claims and secret.
var ErrMismatch = errors.New("approval token does not verify")

// Sign produces the token for the claims. Exported for tests and for the
// evaluation harness; the agent never mints tokens in production.
func Sign(secret []byte, c Claims) string {
	message := strings.Join([]string{Version, c.IncidentID, c.ContextHash, c.Decision, c.Action, c.DecidedAt}, "|")
	mac := hmac.New(sha256.New, secret)
	mac.Write([]byte(message))
	return Version + "." + hex.EncodeToString(mac.Sum(nil))
}

// Verify checks a token against the claims. It is constant time in the
// comparison and refuses a token whose decision is not one that authorises
// an action.
func Verify(secret []byte, token string, c Claims) error {
	if len(secret) == 0 {
		return errors.New("no approval secret configured; nothing can be verified")
	}
	prefix, _, ok := strings.Cut(token, ".")
	if !ok || prefix != Version {
		return ErrMalformed
	}
	if c.Decision != "approved" && c.Decision != "edited" {
		return ErrMismatch
	}
	if !hmac.Equal([]byte(Sign(secret, c)), []byte(token)) {
		return ErrMismatch
	}
	return nil
}
