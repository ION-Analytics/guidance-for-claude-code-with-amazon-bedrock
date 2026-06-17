// Package daemon manages the otelcol lifecycle and emits heartbeat metrics.
// Mirrors the behaviour of source/credential_provider/daemon.py.
//
// Spawned by credential-process --daemon; runs detached as a background
// process. Responsibilities:
//   - Mirror main AWS credentials into the {profile}-collector credentials profile
//   - Start and monitor otelcol
//   - Emit claude_code.daemon.heartbeat via OTLP (localhost:4318)
//   - Emit CollectorHeartbeat directly to CloudWatch via PutMetricData
package daemon

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"gopkg.in/ini.v1"

	"ccwb-go/internal/version"
)

const (
	interval      = 300 * time.Second // 5 minutes between health checks
	cwNamespace   = "ClaudeCode/Security"
	collectorPort = 8888
)

// Run is the main entry point for daemon mode. It blocks until a signal is received.
func Run(profile string, installDir string, cacheDir string) {
	pidFile := filepath.Join(installDir, "daemon.pid")
	collectorPidFile := filepath.Join(installDir, "collector.pid")
	logFile := filepath.Join(cacheDir, "daemon.log")

	// Set up file logging
	lf, err := os.OpenFile(logFile, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0600)
	if err == nil {
		log.SetOutput(lf)
		defer lf.Close()
	}
	log.SetFlags(0) // timestamps formatted manually below
	logger := newLogger()

	// Write PID file
	if err := os.MkdirAll(cacheDir, 0700); err != nil {
		fmt.Fprintf(os.Stderr, "daemon: failed to create cache dir: %v\n", err)
		os.Exit(1)
	}
	if err := os.WriteFile(pidFile, []byte(strconv.Itoa(os.Getpid())+"\n"), 0600); err != nil {
		fmt.Fprintf(os.Stderr, "daemon: failed to write pid file: %v\n", err)
		os.Exit(1)
	}

	logger.infof("daemon started pid=%d profile=%s", os.Getpid(), profile)

	// Signal handling
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGTERM, syscall.SIGINT)
	go func() {
		sig := <-sigCh
		logger.infof("daemon received signal %v, shutting down", sig)
		stopOtelcol(collectorPidFile, logger)
		os.Remove(pidFile)
		os.Exit(0)
	}()

	d := &daemonState{
		profile:          profile,
		installDir:       installDir,
		cacheDir:         cacheDir,
		pidFile:          pidFile,
		collectorPidFile: collectorPidFile,
		logger:           logger,
	}

	// Initial otelcol start
	if !otelcolRunning(collectorPidFile) {
		logger.infof("otelcol not running at startup, starting")
		d.startOtelcol()
		time.Sleep(3 * time.Second)
	}

	// If otelcol still not up (no creds yet), poll at 1s until it starts
	if !otelcolRunning(collectorPidFile) {
		fastTick := time.NewTicker(1 * time.Second)
	fastLoop:
		for range fastTick.C {
			if d.credentialsCached() {
				d.startOtelcol()
				time.Sleep(1 * time.Second)
				fastTick.Stop()
				break fastLoop
			}
		}
	}

	lastCheck := time.Time{}
	tick := time.NewTicker(10 * time.Second)
	defer tick.Stop()

	for range tick.C {
		// Opportunistic start every tick (as soon as credentials available)
		if !otelcolRunning(collectorPidFile) && d.credentialsCached() {
			d.startOtelcol()
		}

		// Proactive restart: if collector credentials expire within 5 minutes,
		// force a credential refresh then restart otelcol with fresh creds.
		if otelcolRunning(collectorPidFile) && d.collectorCredsExpiringSoon() {
			logger.infof("collector credentials expiring soon, forcing refresh and restarting otelcol")
			cp := filepath.Join(d.installDir, "credential-process")
			_ = exec.Command(cp, "--profile", d.profile, "--clear-cache").Run()
			d.writeCollectorCredentials()
			stopOtelcol(collectorPidFile, logger)
			d.startOtelcol()
		}

		now := time.Now()
		if now.Sub(lastCheck) < interval {
			continue
		}
		lastCheck = now

		// Write cred-check timestamp
		_ = os.WriteFile(
			filepath.Join(cacheDir, profile+"-cred-check"),
			[]byte(strconv.FormatInt(now.Unix(), 10)),
			0600,
		)

		if !otelcolRunning(collectorPidFile) {
			logger.infof("otelcol not running, attempting start")
			d.startOtelcol()
		} else if !d.stsValid() {
			logger.infof("STS check failed, refreshing credentials and restarting otelcol")
			cp := filepath.Join(installDir, "credential-process")
			_ = exec.Command(cp, "--profile", profile, "--clear-cache").Run()
			d.writeCollectorCredentials()
			stopOtelcol(collectorPidFile, logger)
			d.startOtelcol()
		} else {
			d.writeCollectorCredentials()
		}

		// Send heartbeat
		email := strings.ToLower(d.readEmail())
		if email != "" {
			d.sendHeartbeat(email, otelcolRunning(collectorPidFile))
		} else {
			logger.infof("no email found, skipping heartbeat")
		}
	}
}

// EnsureRunning checks if the daemon is already running; if not, spawns it
// detached. Safe to call from the credential-process hot path.
func EnsureRunning(profile string, installDir string) {
	pidFile := filepath.Join(installDir, "daemon.pid")
	raw, err := os.ReadFile(pidFile)
	if err == nil {
		pid, err := strconv.Atoi(strings.TrimSpace(string(raw)))
		if err == nil && pid > 0 {
			if processAlive(pid) {
				return // already running
			}
		}
		os.Remove(pidFile)
	}

	// Find the credential-process binary (ourselves)
	self, err := os.Executable()
	if err != nil {
		return
	}

	cmd := exec.Command(self, "--profile", profile, "--daemon") // #nosec G204
	cmd.Stdin = nil
	cmd.Stdout = nil
	cmd.Stderr = nil
	detachProcess(cmd)
	_ = cmd.Start()
}

// ---------------------------------------------------------------------------
// daemon state
// ---------------------------------------------------------------------------

type daemonState struct {
	profile          string
	installDir       string
	cacheDir         string
	pidFile          string
	collectorPidFile string
	logger           *logger
}

func (d *daemonState) credentialsCached() bool {
	creds, err := readCredentials(d.profile)
	if err != nil || creds == nil {
		return false
	}
	return !isExpired(creds)
}

func (d *daemonState) collectorCredentialsValid() bool {
	creds, err := readCredentials(d.profile + "-collector")
	if err != nil || creds == nil {
		return false
	}
	return !isExpired(creds)
}

func (d *daemonState) stsValid() bool {
	return d.credentialsCached() && d.collectorCredentialsValid()
}

func (d *daemonState) collectorCredsExpiringSoon() bool {
	creds, err := readCredentials(d.profile + "-collector")
	if err != nil || creds == nil {
		return false
	}
	if creds.expiration == "" {
		return false
	}
	exp := creds.expiration
	exp = strings.ReplaceAll(exp, "Z", "+00:00")
	t, err := time.Parse(time.RFC3339, exp)
	if err != nil {
		t, err = time.Parse("2006-01-02T15:04:05+00:00", exp)
		if err != nil {
			return false
		}
	}
	return time.Until(t) < 5*time.Minute
}

func (d *daemonState) readEmail() string {
	tokenFile := filepath.Join(d.cacheDir, d.profile+"-monitoring.json")
	raw, err := os.ReadFile(tokenFile)
	if err != nil {
		return ""
	}
	// Parse {"email": "..."} — avoid importing encoding/json to keep binary small.
	// Simple extraction is safe: the file is written by this process.
	s := string(raw)
	key := `"email"`
	idx := strings.Index(s, key)
	if idx < 0 {
		return ""
	}
	after := s[idx+len(key):]
	colon := strings.Index(after, ":")
	if colon < 0 {
		return ""
	}
	after = strings.TrimSpace(after[colon+1:])
	if len(after) == 0 || after[0] != '"' {
		return ""
	}
	end := strings.Index(after[1:], `"`)
	if end < 0 {
		return ""
	}
	return after[1 : end+1]
}

func (d *daemonState) writeCollectorCredentials() {
	creds, err := readCredentials(d.profile)
	if err != nil || creds == nil {
		d.logger.warnf("no credentials found for profile %s, cannot write collector creds", d.profile)
		return
	}
	if isExpiredPlaceholder(creds) {
		d.logger.warnf("credentials for %s are expired, skipping collector write", d.profile)
		return
	}

	collectorProfile := d.profile + "-collector"
	credPath := credentialsFilePath()
	cfg := ini.Empty()
	cfg.ValueMapper = func(s string) string { return s }
	if _, err := os.Stat(credPath); err == nil {
		existing, err := ini.LoadSources(ini.LoadOptions{IgnoreInlineComment: true}, credPath)
		if err == nil {
			cfg = existing
		}
	}

	sec, err := cfg.NewSection(collectorProfile)
	if err != nil {
		// Section already exists — get it
		sec, err = cfg.GetSection(collectorProfile)
		if err != nil {
			d.logger.warnf("failed to get/create collector section: %v", err)
			return
		}
	}
	sec.Key("aws_access_key_id").SetValue(creds.accessKeyID)
	sec.Key("aws_secret_access_key").SetValue(creds.secretAccessKey)
	sec.Key("aws_session_token").SetValue(creds.sessionToken)
	if creds.expiration != "" {
		sec.Key("x-expiration").SetValue(creds.expiration)
	}

	tmpPath := credPath + ".daemon.tmp"
	if err := cfg.SaveTo(tmpPath); err != nil {
		d.logger.warnf("failed to write collector credentials: %v", err)
		return
	}
	_ = os.Chmod(tmpPath, 0600)
	if err := os.Rename(tmpPath, credPath); err != nil {
		_ = os.Remove(tmpPath)
		d.logger.warnf("failed to rename collector credentials: %v", err)
		return
	}
	d.logger.infof("wrote collector credentials for %s", collectorProfile)
}

func (d *daemonState) startOtelcol() {
	otelcol := filepath.Join(d.installDir, otelcolBinary())
	config := filepath.Join(d.installDir, "collector-config.yaml")
	if _, err := os.Stat(otelcol); os.IsNotExist(err) {
		d.logger.infof("otelcol binary not found, skipping")
		return
	}
	if _, err := os.Stat(config); os.IsNotExist(err) {
		d.logger.infof("collector-config.yaml not found, skipping")
		return
	}
	if !d.credentialsCached() {
		d.logger.warnf("no cached credentials found, deferring otelcol start until credentials available")
		return
	}
	d.writeCollectorCredentials()

	env := filterEnv([]string{
		"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
		"AWS_SESSION_TOKEN", "AWS_SESSION_EXPIRATION",
		"AWS_CREDENTIAL_EXPIRATION",
	})
	env = append(env, "AWS_PROFILE="+d.profile+"-collector")

	logPath := filepath.Join(d.cacheDir, "collector.log")
	lf, err := os.OpenFile(logPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0600)
	if err != nil {
		d.logger.warnf("failed to open collector log: %v", err)
		return
	}

	cmd := exec.Command(otelcol, "--config", config) // #nosec G204
	cmd.Env = env
	cmd.Stdout = lf
	cmd.Stderr = lf
	detachProcess(cmd)
	if err := cmd.Start(); err != nil {
		lf.Close()
		d.logger.warnf("failed to start otelcol: %v", err)
		return
	}
	_ = os.WriteFile(d.collectorPidFile, []byte(strconv.Itoa(cmd.Process.Pid)), 0600)
	d.logger.infof("started otelcol pid=%d", cmd.Process.Pid)

	// Don't wait — detached process
	go func() {
		_ = cmd.Wait()
		lf.Close()
	}()
}

func (d *daemonState) sendHeartbeat(email string, otelcolUp bool) {
	// Dot 1: credential-process alive (always sent — we are the credential-process daemon)
	d.sendCloudWatchMetric(email, "CollectorHeartbeat", "UserEmail")
	// Dot 2: otelcol running
	if otelcolUp {
		d.sendCloudWatchMetric(email, "OtelcolHeartbeat", "UserEmail")
	}
	// Version beacon — emitted once per heartbeat cycle so the dashboard can read it
	d.sendCloudWatchMetricWithDims(email, "ClientVersion", map[string]string{
		"UserEmail": email,
		"Version":   version.Version,
	})
}

func (d *daemonState) sendCloudWatchMetric(email string, metricName string, dimName string) {
	creds, err := readCredentials(d.profile)
	if err != nil || creds == nil || isExpiredPlaceholder(creds) {
		d.logger.warnf("credentials unavailable for direct CW heartbeat")
		return
	}

	region := os.Getenv("AWS_REGION")
	if region == "" {
		region = "eu-west-1"
	}

	now := time.Now().UTC()
	amzDate := now.Format("20060102T150405Z")
	dateStamp := now.Format("20060102")
	host := "monitoring." + region + ".amazonaws.com"
	endpoint := "https://" + host + "/"

	body := url.Values{}
	body.Set("Action", "PutMetricData")
	body.Set("Version", "2010-08-01")
	body.Set("Namespace", cwNamespace)
	body.Set("MetricData.member.1.MetricName", metricName)
	body.Set("MetricData.member.1.Value", "1.0")
	body.Set("MetricData.member.1.Unit", "Count")
	body.Set("MetricData.member.1.Dimensions.member.1.Name", dimName)
	body.Set("MetricData.member.1.Dimensions.member.1.Value", email)
	bodyStr := body.Encode()

	payloadHash := sha256Hex(bodyStr)
	headersToSign := map[string]string{
		"content-type":         "application/x-www-form-urlencoded",
		"host":                 host,
		"x-amz-date":          amzDate,
	}
	if creds.sessionToken != "" {
		headersToSign["x-amz-security-token"] = creds.sessionToken
	}

	// Build canonical request
	var headerKeys []string
	for k := range headersToSign {
		headerKeys = append(headerKeys, k)
	}
	sortStrings(headerKeys)
	var canonicalHeaders strings.Builder
	for _, k := range headerKeys {
		canonicalHeaders.WriteString(k + ":" + headersToSign[k] + "\n")
	}
	signedHeaders := strings.Join(headerKeys, ";")
	canonicalRequest := strings.Join([]string{
		"POST", "/", "",
		canonicalHeaders.String(), signedHeaders, payloadHash,
	}, "\n")

	credentialScope := dateStamp + "/" + region + "/monitoring/aws4_request"
	stringToSign := strings.Join([]string{
		"AWS4-HMAC-SHA256", amzDate, credentialScope,
		sha256Hex(canonicalRequest),
	}, "\n")

	signingKey := hmacSHA256(
		hmacSHA256(
			hmacSHA256(
				hmacSHA256([]byte("AWS4"+creds.secretAccessKey), dateStamp),
				region,
			),
			"monitoring",
		),
		"aws4_request",
	)
	signature := fmt.Sprintf("%x", hmac.New(sha256.New, signingKey).Sum(nil))
	// Re-do to get actual signature (Sum appends to empty)
	mac := hmac.New(sha256.New, signingKey)
	mac.Write([]byte(stringToSign))
	signature = fmt.Sprintf("%x", mac.Sum(nil))

	authHeader := fmt.Sprintf(
		"AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s",
		creds.accessKeyID, credentialScope, signedHeaders, signature,
	)

	reqHeaders := map[string]string{
		"Content-Type":         "application/x-www-form-urlencoded",
		"Host":                 host,
		"X-Amz-Date":          amzDate,
		"Authorization":       authHeader,
	}
	if creds.sessionToken != "" {
		reqHeaders["X-Amz-Security-Token"] = creds.sessionToken
	}

	req, err := http.NewRequestWithContext(context.Background(), http.MethodPost, endpoint, strings.NewReader(bodyStr))
	if err != nil {
		d.logger.warnf("direct CW heartbeat request build failed: %v", err)
		return
	}
	for k, v := range reqHeaders {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		d.logger.warnf("direct CW heartbeat failed: %v", err)
		return
	}
	io.Copy(io.Discard, resp.Body)
	resp.Body.Close()
	d.logger.infof("CW %s sent for %s status=%d", metricName, email, resp.StatusCode)
}

func (d *daemonState) sendCloudWatchMetricWithDims(email string, metricName string, dims map[string]string) {
	creds, err := readCredentials(d.profile)
	if err != nil || creds == nil || isExpiredPlaceholder(creds) {
		d.logger.warnf("credentials unavailable for direct CW heartbeat")
		return
	}

	region := os.Getenv("AWS_REGION")
	if region == "" {
		region = "eu-west-1"
	}

	now := time.Now().UTC()
	amzDate := now.Format("20060102T150405Z")
	dateStamp := now.Format("20060102")
	host := "monitoring." + region + ".amazonaws.com"
	endpoint := "https://" + host + "/"

	body := url.Values{}
	body.Set("Action", "PutMetricData")
	body.Set("Version", "2010-08-01")
	body.Set("Namespace", cwNamespace)
	body.Set("MetricData.member.1.MetricName", metricName)
	body.Set("MetricData.member.1.Value", "1.0")
	body.Set("MetricData.member.1.Unit", "Count")

	// Sort dim keys so the body encoding is deterministic (required for SigV4)
	dimKeys := make([]string, 0, len(dims))
	for k := range dims {
		dimKeys = append(dimKeys, k)
	}
	sortStrings(dimKeys)
	for i, k := range dimKeys {
		n := fmt.Sprintf("%d", i+1)
		body.Set("MetricData.member.1.Dimensions.member."+n+".Name", k)
		body.Set("MetricData.member.1.Dimensions.member."+n+".Value", dims[k])
	}
	bodyStr := body.Encode()

	payloadHash := sha256Hex(bodyStr)
	headersToSign := map[string]string{
		"content-type":        "application/x-www-form-urlencoded",
		"host":                host,
		"x-amz-date":         amzDate,
	}
	if creds.sessionToken != "" {
		headersToSign["x-amz-security-token"] = creds.sessionToken
	}

	var headerKeys []string
	for k := range headersToSign {
		headerKeys = append(headerKeys, k)
	}
	sortStrings(headerKeys)
	var canonicalHeaders strings.Builder
	for _, k := range headerKeys {
		canonicalHeaders.WriteString(k + ":" + headersToSign[k] + "\n")
	}
	signedHeaders := strings.Join(headerKeys, ";")
	canonicalRequest := strings.Join([]string{
		"POST", "/", "",
		canonicalHeaders.String(), signedHeaders, payloadHash,
	}, "\n")

	credentialScope := dateStamp + "/" + region + "/monitoring/aws4_request"
	stringToSign := strings.Join([]string{
		"AWS4-HMAC-SHA256", amzDate, credentialScope,
		sha256Hex(canonicalRequest),
	}, "\n")

	signingKey := hmacSHA256(
		hmacSHA256(
			hmacSHA256(
				hmacSHA256([]byte("AWS4"+creds.secretAccessKey), dateStamp),
				region,
			),
			"monitoring",
		),
		"aws4_request",
	)
	mac := hmac.New(sha256.New, signingKey)
	mac.Write([]byte(stringToSign))
	signature := fmt.Sprintf("%x", mac.Sum(nil))

	authHeader := fmt.Sprintf(
		"AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s",
		creds.accessKeyID, credentialScope, signedHeaders, signature,
	)

	reqHeaders := map[string]string{
		"Content-Type":        "application/x-www-form-urlencoded",
		"Host":                host,
		"X-Amz-Date":         amzDate,
		"Authorization":      authHeader,
	}
	if creds.sessionToken != "" {
		reqHeaders["X-Amz-Security-Token"] = creds.sessionToken
	}

	req, err := http.NewRequestWithContext(context.Background(), http.MethodPost, endpoint, strings.NewReader(bodyStr))
	if err != nil {
		d.logger.warnf("direct CW heartbeat request build failed: %v", err)
		return
	}
	for k, v := range reqHeaders {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		d.logger.warnf("direct CW heartbeat failed: %v", err)
		return
	}
	io.Copy(io.Discard, resp.Body)
	resp.Body.Close()
	d.logger.infof("CW %s sent for %s status=%d", metricName, email, resp.StatusCode)
}

// ---------------------------------------------------------------------------
// otelcol helpers
// ---------------------------------------------------------------------------

func otelcolRunning(pidFile string) bool {
	raw, err := os.ReadFile(pidFile)
	if err == nil {
		pid, err := strconv.Atoi(strings.TrimSpace(string(raw)))
		if err == nil && pid > 0 && processAlive(pid) {
			return true
		}
	}
	// Fallback: check port 8888
	conn, err := net.DialTimeout("tcp", "127.0.0.1:"+strconv.Itoa(collectorPort), 500*time.Millisecond)
	if err == nil {
		conn.Close()
		return true
	}
	return false
}

func stopOtelcol(pidFile string, l *logger) {
	raw, err := os.ReadFile(pidFile)
	if err != nil {
		return
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(raw)))
	if err == nil && pid > 0 {
		p, err := os.FindProcess(pid)
		if err == nil {
			_ = p.Signal(syscall.SIGTERM)
			l.infof("stopped otelcol pid=%d", pid)
		}
	}
	os.Remove(pidFile)
}

// ---------------------------------------------------------------------------
// credential helpers
// ---------------------------------------------------------------------------

type awsStaticCreds struct {
	accessKeyID     string
	secretAccessKey string
	sessionToken    string
	expiration      string
}

func credentialsFilePath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".aws", "credentials")
}

func readCredentials(profile string) (*awsStaticCreds, error) {
	cfg, err := ini.LoadSources(ini.LoadOptions{IgnoreInlineComment: true}, credentialsFilePath())
	if err != nil {
		return nil, err
	}
	sec, err := cfg.GetSection(profile)
	if err != nil {
		return nil, nil
	}
	return &awsStaticCreds{
		accessKeyID:     sec.Key("aws_access_key_id").String(),
		secretAccessKey: sec.Key("aws_secret_access_key").String(),
		sessionToken:    sec.Key("aws_session_token").String(),
		expiration:      sec.Key("x-expiration").String(),
	}, nil
}

func isExpired(c *awsStaticCreds) bool {
	if c.accessKeyID == "" || c.secretAccessKey == "" || isExpiredPlaceholder(c) {
		return true
	}
	if c.expiration == "" {
		return false
	}
	exp := c.expiration
	exp = strings.ReplaceAll(exp, "Z", "+00:00")
	t, err := time.Parse(time.RFC3339, exp)
	if err != nil {
		t, err = time.Parse("2006-01-02T15:04:05+00:00", exp)
		if err != nil {
			return false
		}
	}
	return time.Until(t).Seconds() <= 30
}

func isExpiredPlaceholder(c *awsStaticCreds) bool {
	return c.accessKeyID == "EXPIRED"
}

// ---------------------------------------------------------------------------
// OTLP protobuf helpers
// ---------------------------------------------------------------------------


// ---------------------------------------------------------------------------
// misc helpers
// ---------------------------------------------------------------------------

func processAlive(pid int) bool {
	proc, err := os.FindProcess(pid)
	if err != nil {
		return false
	}
	return proc.Signal(syscall.Signal(0)) == nil
}

func filterEnv(exclude []string) []string {
	excl := make(map[string]bool, len(exclude))
	for _, k := range exclude {
		excl[k] = true
	}
	var out []string
	for _, e := range os.Environ() {
		idx := strings.IndexByte(e, '=')
		if idx < 0 {
			continue
		}
		if !excl[e[:idx]] {
			out = append(out, e)
		}
	}
	return out
}

func sortStrings(ss []string) {
	for i := 1; i < len(ss); i++ {
		for j := i; j > 0 && ss[j] < ss[j-1]; j-- {
			ss[j], ss[j-1] = ss[j-1], ss[j]
		}
	}
}

func sha256Hex(s string) string {
	h := sha256.Sum256([]byte(s))
	return fmt.Sprintf("%x", h)
}

func hmacSHA256(key []byte, data string) []byte {
	mac := hmac.New(sha256.New, key)
	mac.Write([]byte(data))
	return mac.Sum(nil)
}

// ---------------------------------------------------------------------------
// logger
// ---------------------------------------------------------------------------

type logger struct{}

func newLogger() *logger { return &logger{} }

func (l *logger) infof(format string, args ...interface{}) {
	msg := fmt.Sprintf(format, args...)
	log.Printf("%s INFO %s", time.Now().Format("2006-01-02T15:04:05-0700"), msg)
}

func (l *logger) warnf(format string, args ...interface{}) {
	msg := fmt.Sprintf(format, args...)
	log.Printf("%s WARNING %s", time.Now().Format("2006-01-02T15:04:05-0700"), msg)
}
