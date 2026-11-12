"""本機假 MCP 伺服器，只給 WOOF 測試用。stdio。"""
from mcp.server import MCPServer

mcp = MCPServer("woof-fake")


@mcp.tool()
def add(a: int, b: int) -> int:
    """兩數相加。"""
    return a + b


@mcp.tool()
def echo(text: str) -> str:
    """原樣回傳文字。"""
    return text


if __name__ == "__main__":
    mcp.run(transport="stdio")
