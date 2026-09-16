#pragma once

#include "mcp/McpTypes.h"

#include <vector>

namespace fincept::mcp::tools {

/// Read-only / research-only tools for the personal Korean-market workflow.
std::vector<ToolDef> get_personal_kr_research_tools();

} // namespace fincept::mcp::tools
