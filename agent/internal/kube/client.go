// Package kube constructs a Kubernetes client for the agent.
//
// The agent is the only component holding cluster credentials. The brain has
// none, which is what makes the approval gate enforceable rather than advisory
// (docs/DESIGN.md section 1).
package kube

import (
	"fmt"
	"os"
	"path/filepath"

	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
)

// Config resolves how the agent reaches the API server.
type Config struct {
	// KubeconfigPath is used when running outside the cluster. Empty means
	// in-cluster credentials first, then the default kubeconfig location.
	KubeconfigPath string
}

// DefaultKubeconfigPath returns $KUBECONFIG, or ~/.kube/config when unset.
func DefaultKubeconfigPath() string {
	if p := os.Getenv("KUBECONFIG"); p != "" {
		return p
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	return filepath.Join(home, ".kube", "config")
}

// RestConfig builds a client-go rest.Config, preferring in-cluster credentials
// so the same binary works in both contexts without a flag.
func RestConfig(c Config) (*rest.Config, error) {
	if c.KubeconfigPath == "" {
		if cfg, err := rest.InClusterConfig(); err == nil {
			return cfg, nil
		}
	}

	path := c.KubeconfigPath
	if path == "" {
		path = DefaultKubeconfigPath()
	}
	if path == "" {
		return nil, fmt.Errorf("no in-cluster credentials and no kubeconfig path could be determined")
	}

	cfg, err := clientcmd.BuildConfigFromFlags("", path)
	if err != nil {
		return nil, fmt.Errorf("loading kubeconfig from %s: %w", path, err)
	}
	return cfg, nil
}

// NewClient returns a typed clientset.
func NewClient(c Config) (kubernetes.Interface, error) {
	cfg, err := RestConfig(c)
	if err != nil {
		return nil, err
	}
	clientset, err := kubernetes.NewForConfig(cfg)
	if err != nil {
		return nil, fmt.Errorf("building clientset: %w", err)
	}
	return clientset, nil
}
