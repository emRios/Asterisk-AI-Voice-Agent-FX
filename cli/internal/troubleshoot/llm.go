package troubleshoot

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"
)

// LLMAnalyzer performs AI-powered diagnosis
type LLMAnalyzer struct {
	provider string
	apiKey   string
	model    string
}

// NewLLMAnalyzer creates an LLM analyzer
func NewLLMAnalyzer() (*LLMAnalyzer, error) {
	// Check for LLM configuration in environment
	provider := os.Getenv("TROUBLESHOOT_LLM_PROVIDER")
	if provider == "" {
		// Try to detect from available keys
		if os.Getenv("OPENAI_API_KEY") != "" {
			provider = "openai"
		} else if os.Getenv("ANTHROPIC_API_KEY") != "" {
			provider = "anthropic"
		} else {
			return nil, fmt.Errorf("no LLM provider configured")
		}
	}

	var apiKey, model string
	switch provider {
	case "openai":
		apiKey = os.Getenv("OPENAI_API_KEY")
		model = "gpt-4o-mini" // Fast and cost-effective
	case "anthropic":
		apiKey = os.Getenv("ANTHROPIC_API_KEY")
		model = "claude-3-haiku-20240307" // Fast and cost-effective
	default:
		return nil, fmt.Errorf("unsupported provider: %s", provider)
	}

	if apiKey == "" {
		return nil, fmt.Errorf("no API key found for provider: %s", provider)
	}

	return &LLMAnalyzer{
		provider: provider,
		apiKey:   apiKey,
		model:    model,
	}, nil
}

// AnalyzeWithLLM performs AI-powered analysis
func (llm *LLMAnalyzer) AnalyzeWithLLM(analysis *Analysis, logData string) (*LLMDiagnosis, error) {
	prompt := llm.buildPrompt(analysis, logData)
	
	var response string
	var err error

	switch llm.provider {
	case "openai":
		response, err = llm.callOpenAI(prompt)
	case "anthropic":
		response, err = llm.callAnthropic(prompt)
	default:
		return nil, fmt.Errorf("unsupported provider: %s", llm.provider)
	}

	if err != nil {
		return nil, err
	}

	return &LLMDiagnosis{
		Provider: llm.provider,
		Model:    llm.model,
		Analysis: response,
	}, nil
}

// buildPrompt constructs the LLM prompt
func (llm *LLMAnalyzer) buildPrompt(analysis *Analysis, logData string) string {
	var prompt strings.Builder
	
	prompt.WriteString("You are an expert in diagnosing Asterisk AI voice agent issues. ")
	prompt.WriteString("Analyze the following call logs and provide a concise diagnosis.\n\n")
	
	prompt.WriteString("Call ID: " + analysis.CallID + "\n\n")
	
	// Pipeline status
	prompt.WriteString("Pipeline Status:\n")
	prompt.WriteString(fmt.Sprintf("- AudioSocket: %v\n", analysis.HasAudioSocket))
	prompt.WriteString(fmt.Sprintf("- Transcription: %v\n", analysis.HasTranscription))
	prompt.WriteString(fmt.Sprintf("- Playback: %v\n", analysis.HasPlayback))
	prompt.WriteString("\n")
	
	// Issues found
	if len(analysis.Errors) > 0 {
		prompt.WriteString(fmt.Sprintf("Errors found: %d\n", len(analysis.Errors)))
		count := 5
		if len(analysis.Errors) < 5 {
			count = len(analysis.Errors)
		}
		for i := 0; i < count; i++ {
			prompt.WriteString(fmt.Sprintf("- %s\n", truncate(analysis.Errors[i], 200)))
		}
		prompt.WriteString("\n")
	}
	
	if len(analysis.AudioIssues) > 0 {
		prompt.WriteString("Audio Issues:\n")
		for _, issue := range analysis.AudioIssues {
			prompt.WriteString(fmt.Sprintf("- %s\n", issue))
		}
		prompt.WriteString("\n")
	}
	
	// Symptom if specified
	if analysis.Symptom != "" {
		prompt.WriteString(fmt.Sprintf("Reported Symptom: %s\n\n", analysis.Symptom))
	}
	
	// Extracted metrics (CRITICAL for diagnosis)
	if analysis.Metrics != nil {
		prompt.WriteString(analysis.Metrics.FormatForLLM())
	}
	
	// Sample logs (truncated)
	prompt.WriteString("Sample Log Lines:\n")
	lines := strings.Split(logData, "\n")
	count := 10
	if len(lines) < 10 {
		count = len(lines)
	}
	for i := 0; i < count; i++ {
		if lines[i] != "" {
			prompt.WriteString(truncate(lines[i], 200) + "\n")
		}
	}
	prompt.WriteString("\n")
	
	prompt.WriteString("Please provide:\n")
	prompt.WriteString("1. Root Cause: What is the most likely root cause based on the metrics?\n")
	prompt.WriteString("   - Focus on provider_bytes ratio (should be 1.0)\n")
	prompt.WriteString("   - Check drift_pct (should be <10%)\n")
	prompt.WriteString("   - Note underflow events\n")
	prompt.WriteString("   - VAD aggressiveness level (0 = too sensitive)\n")
	prompt.WriteString("   - Gate flutter (>50 closures = problem)\n")
	prompt.WriteString("   - Format mismatches\n")
	prompt.WriteString("2. Confidence: How confident are you? (High/Medium/Low)\n")
	prompt.WriteString("3. Quick Fix: What's the immediate action to take?\n")
	prompt.WriteString("4. Prevention: How to prevent this in the future?\n")
	prompt.WriteString("\nKeep your response concise and actionable (under 400 words).")
	
	return prompt.String()
}

// callOpenAI makes OpenAI API request
func (llm *LLMAnalyzer) callOpenAI(prompt string) (string, error) {
	url := "https://api.openai.com/v1/chat/completions"
	
	requestBody := map[string]interface{}{
		"model": llm.model,
		"messages": []map[string]string{
			{
				"role":    "user",
				"content": prompt,
			},
		},
		"max_tokens":  800,
		"temperature": 0.3,
	}
	
	jsonData, err := json.Marshal(requestBody)
	if err != nil {
		return "", err
	}
	
	req, err := http.NewRequest("POST", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return "", err
	}
	
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+llm.apiKey)
	
	client := &http.Client{Timeout: 30 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return "", fmt.Errorf("OpenAI request failed: %w", err)
	}
	defer resp.Body.Close()
	
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", err
	}
	
	if resp.StatusCode != 200 {
		return "", fmt.Errorf("OpenAI API error %d: %s", resp.StatusCode, string(body))
	}
	
	var result map[string]interface{}
	if err := json.Unmarshal(body, &result); err != nil {
		return "", err
	}
	
	choices, ok := result["choices"].([]interface{})
	if !ok || len(choices) == 0 {
		return "", fmt.Errorf("no response from OpenAI")
	}
	
	message, ok := choices[0].(map[string]interface{})["message"].(map[string]interface{})
	if !ok {
		return "", fmt.Errorf("invalid response format")
	}
	
	content, ok := message["content"].(string)
	if !ok {
		return "", fmt.Errorf("no content in response")
	}
	
	return content, nil
}

// callAnthropic makes Anthropic API request
func (llm *LLMAnalyzer) callAnthropic(prompt string) (string, error) {
	url := "https://api.anthropic.com/v1/messages"
	
	requestBody := map[string]interface{}{
		"model": llm.model,
		"messages": []map[string]string{
			{
				"role":    "user",
				"content": prompt,
			},
		},
		"max_tokens": 800,
	}
	
	jsonData, err := json.Marshal(requestBody)
	if err != nil {
		return "", err
	}
	
	req, err := http.NewRequest("POST", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return "", err
	}
	
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("x-api-key", llm.apiKey)
	req.Header.Set("anthropic-version", "2023-06-01")
	
	client := &http.Client{Timeout: 30 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return "", fmt.Errorf("Anthropic request failed: %w", err)
	}
	defer resp.Body.Close()
	
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", err
	}
	
	if resp.StatusCode != 200 {
		return "", fmt.Errorf("Anthropic API error %d: %s", resp.StatusCode, string(body))
	}
	
	var result map[string]interface{}
	if err := json.Unmarshal(body, &result); err != nil {
		return "", err
	}
	
	content, ok := result["content"].([]interface{})
	if !ok || len(content) == 0 {
		return "", fmt.Errorf("no content in response")
	}
	
	text, ok := content[0].(map[string]interface{})["text"].(string)
	if !ok {
		return "", fmt.Errorf("invalid response format")
	}
	
	return text, nil
}

// LLMDiagnosis holds LLM analysis results
type LLMDiagnosis struct {
	Provider string
	Model    string
	Analysis string
}
