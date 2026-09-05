package contract

import (
	"fmt"
	"time"
)

// FormatAggregation renders an event's occurrence count in the form kubectl
// uses in describe output, for example "x5 over 2m42s".
//
// The structured fields are what the contract stores, but this phrasing is
// reconstructed for the prompt: it is compact and heavily represented in
// training data, which makes it more legible to a model than three ISO
// timestamps (docs/DESIGN.md section 3.3).
func FormatAggregation(count int32, first, last time.Time) string {
	if count <= 1 {
		return ""
	}
	if first.IsZero() || last.IsZero() {
		return fmt.Sprintf("x%d", count)
	}
	d := last.Sub(first).Truncate(time.Second)
	if d <= 0 {
		return fmt.Sprintf("x%d", count)
	}
	return fmt.Sprintf("x%d over %s", count, d)
}

// CrashesPerMinute derives the flap rate. Returns 0 for a non-positive age so
// a freshly observed pod does not report an infinite rate.
func CrashesPerMinute(restarts int32, age time.Duration) float64 {
	if restarts <= 0 || age <= 0 {
		return 0
	}
	return float64(restarts) / age.Minutes()
}
