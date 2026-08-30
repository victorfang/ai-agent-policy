# Only CEO or CFO may call the financial P&L tool.
# Author: Victor Fang, 2026
package pnl

default allow := false

allow if {
	input.tool == "get_pnl"
	input.user.role in {"CEO", "CFO"}
}
