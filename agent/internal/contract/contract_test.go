package contract

import (
	"testing"
	"time"
)

func TestTimeOrNil(t *testing.T) {
	real := time.Date(2026, 9, 6, 12, 58, 19, 0, time.UTC)

	cases := []struct {
		name string
		in   time.Time
		want *time.Time
	}{
		{"go zero time is absent", time.Time{}, nil},
		// Recorded live: startedAt on a container whose init was killed.
		{"unix epoch is absent", time.Unix(0, 0).UTC(), nil},
		{"unix epoch in another zone is absent", time.Unix(0, 0).In(time.FixedZone("PKT", 5*3600)), nil},
		{"a real timestamp is kept", real, &real},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := TimeOrNil(tc.in)
			switch {
			case tc.want == nil && got != nil:
				t.Errorf("TimeOrNil(%v) = %v, want nil", tc.in, *got)
			case tc.want != nil && (got == nil || !got.Equal(*tc.want)):
				t.Errorf("TimeOrNil(%v) = %v, want %v", tc.in, got, *tc.want)
			}
		})
	}
}
