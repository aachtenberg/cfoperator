package tui

import "github.com/charmbracelet/lipgloss"

var (
	// Input area
	inputStyle = lipgloss.NewStyle().
			Background(lipgloss.Color("#12121a")).
			Foreground(lipgloss.Color("#e0e0e8")).
			Padding(0, 1)

	// Status bar
	statusStyle = lipgloss.NewStyle().
			Background(lipgloss.Color("#12121a")).
			Foreground(lipgloss.Color("#8888a0")).
			Padding(0, 1)

	// Separator
	separatorStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("#2a2a3a"))

	// User prompt in output
	userPromptStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("#22c55e")).
			Bold(true)

	// Tool call
	toolNameStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("#06b6d4")).
			Faint(true)

	// Tool result — success
	toolSuccessStyle = lipgloss.NewStyle().
				Foreground(lipgloss.Color("#22c55e")).
				Faint(true)

	// Tool result — error
	toolErrorStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("#ef4444")).
			Faint(true)

	// Banner
	bannerStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("#22c55e")).
			Bold(true)

	bannerDimStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("#166534"))

	// Info/dim text
	dimStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("#555566"))

	// Error
	errorStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("#ef4444")).
			Bold(true)

	// Warning
	warningStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("#f59e0b"))
)

// The Palette (CFOP-69): the input's box, the menu under it, the footer.
var (
	// Box border: green at rest, amber while a turn runs. Colour reads from
	// across the room; a word in a status bar does not.
	boxIdleStyle = lipgloss.NewStyle().
			Border(lipgloss.RoundedBorder()).
			BorderForeground(lipgloss.Color("#166534"))

	boxBusyStyle = lipgloss.NewStyle().
			Border(lipgloss.RoundedBorder()).
			BorderForeground(lipgloss.Color("#b45309"))

	// The attachment title, written into the top border.
	boxTitleStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("#8888a0"))

	// Menu rows
	menuLabelStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("#e0e0e8"))

	menuSelectedStyle = lipgloss.NewStyle().
				Background(lipgloss.Color("#1e2b3c")).
				Foreground(lipgloss.Color("#e0e0e8"))

	// Footer: hints left, status right, both quiet.
	footerHintStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("#555566"))

	footerStatusStyle = lipgloss.NewStyle().
				Foreground(lipgloss.Color("#8888a0"))
)
