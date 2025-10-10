#!/usr/bin/env python3
"""
async_calculator_server.py - Async Calculator MCP server example
Demonstrates mixed sync/async tool implementation
"""

from asyncio import sleep
from typing import Any, Dict

from aioumcp import AsyncMCPServer


class AsyncCalculatorMCPServer(AsyncMCPServer):
    """Calculator MCP server with both sync and async operations."""
    
    def get_instructions(self) -> str:
        """Get server-specific instructions."""
        return "This server provides basic calculator functionality with async support for demonstration."
    
    # Synchronous tools (will run in thread pool)
    def tool_add(self, a: float, b: float) -> Dict[str, Any]:
        """Add two numbers together (synchronous).
        
        Args:
            a: First number
            b: Second number
            
        Returns:
            Result of addition operation
        """
        try:
            result = a + b
            return {"operation": "addition", "a": a, "b": b, "result": result, "async": False}
        except (ValueError, TypeError) as e:
            self.logger.error("Error in add operation: %s", e)
            return {"error": "Invalid input for addition"}
    
    def tool_subtract(self, a: float, b: float) -> Dict[str, Any]:
        """Subtract second number from first number (synchronous).
        
        Args:
            a: First number (minuend)
            b: Second number (subtrahend)
            
        Returns:
            Result of subtraction operation
        """
        try:
            result = a - b
            return {"operation": "subtraction", "a": a, "b": b, "result": result, "async": False}
        except (ValueError, TypeError) as e:
            self.logger.error("Error in subtract operation: %s", e)
            return {"error": "Invalid input for subtraction"}
    
    # Asynchronous tools (demonstrate I/O-bound operations)
    async def tool_multiply_async(self, a: float, b: float) -> Dict[str, Any]:
        """Multiply two numbers (asynchronous with simulated delay).
        
        Args:
            a: First number
            b: Second number
            
        Returns:
            Result of multiplication operation
        """
        try:
            # Simulate async I/O operation (e.g., database query, API call)
            await sleep(0.1)  # Simulated delay
            result = a * b
            return {"operation": "multiplication", "a": a, "b": b, "result": result, "async": True}
        except (ValueError, TypeError) as e:
            self.logger.error("Error in multiply operation: %s", e)
            return {"error": "Invalid input for multiplication"}
    
    async def tool_divide_async(self, a: float, b: float) -> Dict[str, Any]:
        """Divide first number by second number (asynchronous).
        
        Args:
            a: Dividend
            b: Divisor (cannot be zero)
            
        Returns:
            Result of division operation
        """
        try:
            if b == 0:
                return {"error": "Division by zero is not allowed"}
            
            # Simulate async operation
            await sleep(0.05)
            result = a / b
            return {"operation": "division", "a": a, "b": b, "result": result, "async": True}
        except (ValueError, TypeError) as e:
            self.logger.error("Error in divide operation: %s", e)
            return {"error": "Invalid input for division"}
    
    async def tool_power_async(self, base: float, exponent: float) -> Dict[str, Any]:
        """Calculate base raised to the power of exponent (asynchronous).
        
        Args:
            base: Base number
            exponent: Exponent
            
        Returns:
            Result of power operation
        """
        try:
            # Simulate complex calculation that benefits from async
            await sleep(0.2)  # Longer delay for complex operation
            result = base ** exponent
            return {"operation": "power", "base": base, "exponent": exponent, "result": result, "async": True}
        except (ValueError, TypeError, OverflowError) as e:
            self.logger.error("Error in power operation: %s", e)
            return {"error": "Invalid input for power operation"}
    
    async def tool_factorial_async(self, n: int) -> Dict[str, Any]:
        """Calculate factorial of a number (asynchronous with yielding).
        
        Args:
            n: Number to calculate factorial for
            
        Returns:
            Result of factorial operation
        """
        try:
            if n < 0:
                return {"error": "Factorial is not defined for negative numbers"}
            
            if n > 20:  # Prevent very large calculations
                return {"error": "Number too large for factorial calculation"}
            
            result = 1
            for i in range(1, n + 1):
                result *= i
                # Yield control periodically for large calculations
                if i % 5 == 0:
                    await sleep(0.01)
            
            return {"operation": "factorial", "n": n, "result": result, "async": True}
        except (ValueError, TypeError) as e:
            self.logger.error("Error in factorial operation: %s", e)
            return {"error": "Invalid input for factorial"}
    
    def prompt_calculate_product(self, a: float, b: float) -> str:
        """Generate a prompt for calculating the product of two numbers.
        Categories: math, calculation
        """
        return f"What is the product of {a} and {b}?"

    async def prompt_calculate_quotient(self, a: float, b: float) -> str:
        """Generate a prompt for calculating the quotient of two numbers.
        Categories: math, calculation
        """
        if b == 0:
            return "Division by zero is not allowed."
        return f"What is the result of dividing {a} by {b}?"


if __name__ == "__main__":
    # Start the Async Calculator MCP server
    server = AsyncCalculatorMCPServer()
    server.run()
