package tui

import "github.com/aachtenberg/cfoperator/cfassist-go/internal/conversation"

// llmDoneMsg is sent when the LLM conversation turn completes.
type llmDoneMsg struct {
	result conversation.Result
}

// errMsg is sent on errors.
type errMsg struct {
	err error
}

// appendOutputMsg is sent from the conversation output callbacks
// to append text to the viewport.
type appendOutputMsg struct {
	text string
}
