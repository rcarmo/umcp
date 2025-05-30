#!/usr/bin/env python3
"""
moviemcpserver.py - Movie business logic implementation
"""

from time import time
from typing import Any, Dict, List, Optional

from umcp import MCPServer


class MovieMCPServer(MCPServer):
    """Movie MCP server with ticket booking functionality."""
    
    def __init__(self):
        super().__init__()
        self.movies_data = [
            {"id": 1, "title": "Avengers: Endgame", "showTimes": ["10:00", "13:30", "17:00", "20:30"], "price": 12.99},
            {"id": 2, "title": "The Matrix Resurrections", "showTimes": ["11:00", "14:00", "18:30", "21:00"], "price": 11.99},
            {"id": 3, "title": "Dune", "showTimes": ["10:30", "13:00", "16:30", "20:00"], "price": 12.99},
            {"id": 4, "title": "No Time to Die", "showTimes": ["11:30", "15:00", "18:00", "21:30"], "price": 13.99}
        ]
    
    def get_instructions(self) -> str:
        """Get server-specific instructions."""
        return "This server provides movie information and ticket booking capabilities. You can list movies and book tickets."
    
    def tool_get_movies(self) -> List[Dict[str, Any]]:
        """Get a list of movies currently playing in theaters."""
        return self.movies_data
    
    def tool_book_ticket(self, movie_id: int, show_time: str, num_tickets: int) -> Optional[Dict[str, Any]]:
        """Book movie tickets for a specific showing.
        
        Args:
            movie_id: ID of the movie to book
            show_time: Show time (e.g., '14:30')  
            num_tickets: Number of tickets to book
                
        Returns:
            Booking confirmation dictionary or None if booking fails
        """
        try:
            # Validate inputs
            if (movie_id is None or not isinstance(movie_id, int) or movie_id <= 0 or
                show_time is None or not isinstance(show_time, str) or not show_time.strip() or
                num_tickets is None or not isinstance(num_tickets, int) or num_tickets <= 0):
                return None
            
            # Generate booking confirmation
            total_price = num_tickets * 12.99
            booking_id = f"BK{int(time())}"
            
            return {
                "bookingId": booking_id,
                "movieId": movie_id,
                "showTime": show_time,
                "numTickets": num_tickets,
                "totalPrice": total_price
            }
            
        except (ValueError, TypeError, KeyError) as e:
            self.logger.error("Error booking ticket: %s", e)
            return None
    
    def tool_get_showtimes(self, movie_id: int) -> List[str]:
        """Get showtimes for a specific movie.
        
        Args:
            movie_id: ID of the movie to get showtimes for
            
        Returns:
            List of showtimes for the specified movie
        """
        for movie in self.movies_data:
            if movie["id"] == movie_id:
                return movie["showTimes"]
        return []
    
    def tool_search_movies(self, title: Optional[str] = None, min_price: Optional[float] = None, max_price: Optional[float] = None) -> List[Dict[str, Any]]:
        """Search for movies by title and/or price range.
        
        Args:
            title: Optional title to search for (case-insensitive partial match)
            min_price: Optional minimum price
            max_price: Optional maximum price
            
        Returns:
            List of matching movies
        """
        results = []
        
        for movie in self.movies_data:
            match = True
            
            # Check title if provided
            if title and title.lower() not in movie["title"].lower():
                match = False
                
            # Check min price if provided
            if min_price is not None and movie["price"] < min_price:
                match = False
                
            # Check max price if provided
            if max_price is not None and movie["price"] > max_price:
                match = False
                
            if match:
                results.append(movie)
                
        return results


if __name__ == "__main__":
    # Start the MCP server
    server = MovieMCPServer()
    server.run()
