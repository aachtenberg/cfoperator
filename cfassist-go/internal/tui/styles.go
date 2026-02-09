package tui

import "github.com/charmbracelet/lipgloss"

var (
	// Input area — transparent dark background
	inputStyle = lipgloss.NewStyle().
			Background(lipgloss.Color("#0d0d1a")).
			Foreground(lipgloss.Color("#cccccc")).
			Padding(0, 1)

	// Status bar
	statusStyle = lipgloss.NewStyle().
			Background(lipgloss.Color("#1a1a2e")).
			Foreground(lipgloss.Color("#888888")).
			Padding(0, 1)

	// Separator
	separatorStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("#333333"))

	// User prompt in output
	userPromptStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("#00aa00")).
			Bold(true)

	// Tool call
	toolNameStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("#008888")).
			Faint(true)

	// Tool result — success
	toolSuccessStyle = lipgloss.NewStyle().
				Foreground(lipgloss.Color("#00aa00")).
				Faint(true)

	// Tool result — error
	toolErrorStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("#aa0000")).
			Faint(true)

	// Banner
	bannerStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("#00aa00")).
			Bold(true)

	bannerDimStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("#00aa00")).
			Faint(true)

	// Info/dim text
	dimStyle = lipgloss.NewStyle().Faint(true)

	// Error
	errorStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("#ff0000")).
			Bold(true)

	// Warning
	warningStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("#ffaa00"))
)
