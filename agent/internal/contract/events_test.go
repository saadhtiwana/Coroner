package contract

import (
	"math"
	"testing"
	"time"
)

func TestFormatAggregation(t *testing.T) {
	base := time.Date(2026, 9, 5, 18, 0, 0, 0, time.UTC)

	cases := []struct {
		name  string
		count int32
		first time.Time
		last  time.Time
		want  string
	}{
		{"single occurrence is not aggregated", 1, base, base.Add(time.Minute), ""},
		{"zero count is not aggregated", 0, base, base, ""},
		{"matches kubectl phrasing", 5, base, base.Add(162 * time.Second), "x5 over 2m42s"},
		{"sub-minute window", 3, base, base.Add(7 * time.Second), "x3 over 7s"},
		{"fractional seconds are truncated", 4, base, base.Add(2500 * time.Millisecond), "x4 over 2s"},
		{"identical timestamps omit the window", 9, base, base, "x9"},
		{"zero timestamps omit the window", 2, time.Time{}, time.Time{}, "x2"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := FormatAggregation(tc.count, tc.first, tc.last); got != tc.want {
				t.Errorf("FormatAggregation(%d) = %q, want %q", tc.count, got, tc.want)
			}
		})
	}
}

func TestCrashesPerMinute(t *testing.T) {
	cases := []struct {
		name     string
		restarts int32
		age      time.Duration
		want     float64
	}{
		{"four restarts over two minutes", 4, 2 * time.Minute, 2},
		{"no restarts", 0, 5 * time.Minute, 0},
		{"zero age does not divide by zero", 3, 0, 0},
		{"negative age is rejected", 3, -time.Minute, 0},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := CrashesPerMinute(tc.restarts, tc.age)
			if math.Abs(got-tc.want) > 1e-9 {
				t.Errorf("CrashesPerMinute(%d, %v) = %v, want %v", tc.restarts, tc.age, got, tc.want)
			}
		})
	}
}
