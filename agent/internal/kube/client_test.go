package kube

import (
	"os"
	"path/filepath"
	"testing"
)

func TestDefaultKubeconfigPathPrefersEnv(t *testing.T) {
	t.Setenv("KUBECONFIG", "/tmp/explicit-kubeconfig")
	if got := DefaultKubeconfigPath(); got != "/tmp/explicit-kubeconfig" {
		t.Errorf("DefaultKubeconfigPath() = %q, want the KUBECONFIG value", got)
	}
}

func TestDefaultKubeconfigPathFallsBackToHome(t *testing.T) {
	t.Setenv("KUBECONFIG", "")
	home, err := os.UserHomeDir()
	if err != nil {
		t.Skip("no home directory available")
	}
	want := filepath.Join(home, ".kube", "config")
	if got := DefaultKubeconfigPath(); got != want {
		t.Errorf("DefaultKubeconfigPath() = %q, want %q", got, want)
	}
}

func TestRestConfigRejectsMissingKubeconfig(t *testing.T) {
	_, err := RestConfig(Config{KubeconfigPath: filepath.Join(t.TempDir(), "absent")})
	if err == nil {
		t.Fatal("RestConfig() returned no error for a nonexistent kubeconfig")
	}
}
