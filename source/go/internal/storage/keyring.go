package storage

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"

	"github.com/99designs/keyring"
	"ccwb-go/internal/federation"
)

const serviceName = "claude-code-with-bedrock"

func openKeyring() (keyring.Keyring, error) {
	cfg := keyring.Config{
		ServiceName: serviceName,
		// macOS Keychain
		KeychainName:             "login",
		KeychainTrustApplication: true,
		// Linux Secret Service
		LibSecretCollectionName: serviceName,
		// Windows Credential Manager
		WinCredPrefix: serviceName,
	}
	// Pin to the native backend for the current OS so the library never falls
	// through to 'pass' or other CLI-based backends that may be installed.
	switch runtime.GOOS {
	case "darwin":
		cfg.AllowedBackends = []keyring.BackendType{keyring.KeychainBackend}
	case "linux":
		cfg.AllowedBackends = []keyring.BackendType{keyring.SecretServiceBackend}
	case "windows":
		cfg.AllowedBackends = []keyring.BackendType{keyring.WinCredBackend}
	}
	return keyring.Open(cfg)
}

// ReadFromKeyring reads AWS credentials from the OS keyring.
func ReadFromKeyring(profile string) (*federation.AWSCredentials, error) {
	kr, err := openKeyring()
	if err != nil {
		return nil, err
	}

	if runtime.GOOS == "windows" {
		return readFromKeyringWindows(kr, profile)
	}

	item, err := kr.Get(profile + "-credentials")
	if err != nil {
		return nil, err
	}

	var creds federation.AWSCredentials
	if err := json.Unmarshal(item.Data, &creds); err != nil {
		return nil, err
	}
	return &creds, nil
}

// SaveToKeyring saves AWS credentials to the OS keyring.
func SaveToKeyring(creds *federation.AWSCredentials, profile string) error {
	kr, err := openKeyring()
	if err != nil {
		return err
	}

	if runtime.GOOS == "windows" {
		return saveToKeyringWindows(kr, creds, profile)
	}

	data, err := json.Marshal(creds)
	if err != nil {
		return err
	}

	return kr.Set(keyring.Item{
		Key:  profile + "-credentials",
		Data: data,
	})
}

// ClearKeyring replaces credentials with an expired dummy to maintain keychain permissions.
func ClearKeyring(profile string) error {
	expired := &federation.AWSCredentials{
		Version:         1,
		AccessKeyID:     "EXPIRED",
		SecretAccessKey: "EXPIRED",
		SessionToken:    "EXPIRED",
		Expiration:      "2000-01-01T00:00:00Z",
	}
	return SaveToKeyring(expired, profile)
}

// ReadClientSecret reads an Azure confidential-client secret.
// When storageType is "keyring" it reads from the OS keyring (matches what the
// Python ccwb init wizard writes). Otherwise it reads from the session file at
// ~/.claude-code-session/{profile}-client-secret (CGO-free, session storage).
func ReadClientSecret(profile, storageType string) (string, error) {
	if storageType == "keyring" {
		kr, err := openKeyring()
		if err != nil {
			return "", err
		}
		item, err := kr.Get(profile + "-client-secret")
		if err != nil {
			if errors.Is(err, keyring.ErrKeyNotFound) {
				return "", nil
			}
			return "", err
		}
		return string(item.Data), nil
	}
	// session / file storage
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	data, err := os.ReadFile(filepath.Join(home, ".claude-code-session", profile+"-client-secret"))
	if err != nil {
		if os.IsNotExist(err) {
			return "", nil
		}
		return "", err
	}
	return strings.TrimRight(string(data), "\r\n"), nil
}

// WriteClientSecret stores an Azure confidential-client secret.
// When storageType is "keyring" it writes to the OS keyring. Otherwise it writes
// to ~/.claude-code-session/{profile}-client-secret with 0600 permissions.
func WriteClientSecret(profile, storageType, secret string) error {
	if storageType == "keyring" {
		kr, err := openKeyring()
		if err != nil {
			return err
		}
		return kr.Set(keyring.Item{
			Key:  profile + "-client-secret",
			Data: []byte(secret),
		})
	}
	// session / file storage
	home, err := os.UserHomeDir()
	if err != nil {
		return err
	}
	dir := filepath.Join(home, ".claude-code-session")
	if err := os.MkdirAll(dir, 0700); err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(dir, profile+"-client-secret"), []byte(secret), 0600)
}

// ReadMonitoringTokenFromKeyring reads the monitoring token from keyring.
func ReadMonitoringTokenFromKeyring(profile string) (*MonitoringTokenData, error) {
	kr, err := openKeyring()
	if err != nil {
		return nil, err
	}

	item, err := kr.Get(profile + "-monitoring")
	if err != nil {
		return nil, err
	}

	var data MonitoringTokenData
	if err := json.Unmarshal(item.Data, &data); err != nil {
		return nil, err
	}
	return &data, nil
}

// SaveMonitoringTokenToKeyring saves a monitoring token to keyring.
func SaveMonitoringTokenToKeyring(data *MonitoringTokenData, profile string) error {
	kr, err := openKeyring()
	if err != nil {
		return err
	}

	jsonData, err := json.Marshal(data)
	if err != nil {
		return err
	}

	return kr.Set(keyring.Item{
		Key:  profile + "-monitoring",
		Data: jsonData,
	})
}

// MonitoringTokenData represents the monitoring token stored in keyring or file.
type MonitoringTokenData struct {
	Token   string `json:"token"`
	Expires int64  `json:"expires"`
	Email   string `json:"email"`
	Profile string `json:"profile"`
}
