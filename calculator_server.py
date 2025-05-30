#!/usr/bin/env python3
"""
caulculator_server.py - Calculator MCP server example
Demonstrates pure Python MCP server without JSON configuration files
"""

from typing import Any, Dict

from umcp import MCPServer


class CalculatorMCPServer(MCPServer):
    """Calculator MCP server with basic math operations."""
    
    def get_instructions(self) -> str:
        """Get server-specific instructions."""
        return "This server provides basic calculator functionality including addition, subtraction, multiplication, and division."
    
    def tool_add(self, a: float, b: float) -> Dict[str, Any]:
        """Add two numbers together.
        
        Args:
            a: First number
            b: Second number
            
        Returns:
            Result of addition operation
        """
        try:
            result = a + b
            return {"operation": "addition", "a": a, "b": b, "result": result}
        except (ValueError, TypeError) as e:
            self.logger.error("Error in add operation: %s", e)
            return {"error": "Invalid input for addition"}
    
    def tool_subtract(self, a: float, b: float) -> Dict[str, Any]:
        """Subtract second number from first number.
        
        Args:
            a: First number (minuend)
            b: Second number (subtrahend)
            
        Returns:
            Result of subtraction operation
        """
        try:
            result = a - b
            return {"operation": "subtraction", "a": a, "b": b, "result": result}
        except (ValueError, TypeError) as e:
            self.logger.error("Error in subtract operation: %s", e)
            return {"error": "Invalid input for subtraction"}
    
    def tool_multiply(self, a: float, b: float) -> Dict[str, Any]:
        """Multiply two numbers.
        
        Args:
            a: First number
            b: Second number
            
        Returns:
            Result of multiplication operation
        """
        try:
            result = a * b
            return {"operation": "multiplication", "a": a, "b": b, "result": result}
        except (ValueError, TypeError) as e:
            self.logger.error("Error in multiply operation: %s", e)
            return {"error": "Invalid input for multiplication"}
    
    def tool_divide(self, a: float, b: float) -> Dict[str, Any]:
        """Divide first number by second number.
        
        Args:
            a: Dividend
            b: Divisor (cannot be zero)
            
        Returns:
            Result of division operation
        """
        try:
            if b == 0:
                return {"error": "Division by zero is not allowed"}
            
            result = a / b
            return {"operation": "division", "a": a, "b": b, "result": result}
        except (ValueError, TypeError) as e:
            self.logger.error("Error in divide operation: %s", e)
            return {"error": "Invalid input for division"}


if __name__ == "__main__":
    # Start the Calculator MCP server
    server = CalculatorMCPServer()
    server.run()
