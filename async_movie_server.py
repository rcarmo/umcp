#!/usr/bin/env python3
"""
async_movie_server.py - Async Movie MCP server with simulated API calls
Demonstrates async I/O operations and concurrent execution
"""

from asyncio import gather, sleep
from time import time
from typing import Any, Dict, List

from aioumcp import AsyncMCPServer


class AsyncMovieMCPServer(AsyncMCPServer):
    """Movie MCP server with async ticket booking and API simulation."""
    
    def __init__(self):
        super().__init__()
        self.movies_data = [
            {"id": 1, "title": "Avengers: Endgame", "showTimes": ["10:00", "13:30", "17:00", "20:30"], "price": 12.99},
            {"id": 2, "title": "The Matrix Resurrections", "showTimes": ["11:00", "14:00", "18:30", "21:00"], "price": 11.99},
            {"id": 3, "title": "Dune", "showTimes": ["10:30", "13:00", "16:30", "20:00"], "price": 12.99},
            {"id": 4, "title": "No Time to Die", "showTimes": ["11:30", "15:00", "18:00", "21:30"], "price": 13.99}
        ]
        self.bookings = {}  # Store bookings in memory
    
    def get_instructions(self) -> str:
        """Get server-specific instructions."""
        return "This async movie server provides movie information, ticket booking, and external API integration with concurrent execution support."
    
    # Synchronous tool for basic data retrieval
    def tool_get_movies(self) -> List[Dict[str, Any]]:
        """Get a list of movies currently playing in theaters (synchronous)."""
        return {
            "movies": self.movies_data,
            "retrieved_at": time(),
            "async": False
        }
    
    # Async tool with simulated database/API call
    async def tool_get_movie_details_async(self, movie_id: int) -> Dict[str, Any]:
        """Get detailed information about a specific movie (asynchronous with API simulation).
        
        Args:
            movie_id: ID of the movie to get details for
            
        Returns:
            Detailed movie information
        """
        try:
            # Simulate API call delay
            await sleep(0.5)
            
            # Find movie in our data
            movie = next((m for m in self.movies_data if m["id"] == movie_id), None)
            if not movie:
                return {"error": f"Movie with ID {movie_id} not found"}
            
            # Simulate fetching additional details from external API
            await sleep(0.3)  # Simulate second API call
            
            # Return enhanced movie data
            enhanced_movie = movie.copy()
            enhanced_movie.update({
                "director": "Sample Director",
                "genre": ["Action", "Adventure"],
                "rating": "PG-13",
                "duration": "180 minutes",
                "description": f"This is the description for {movie['title']}",
                "retrieved_at": time(),
                "async": True
            })
            
            return enhanced_movie
            
        except (ValueError, TypeError) as e:
            self.logger.error("Error getting movie details for %s: %s", movie_id, e)
            return {"error": f"Invalid movie ID: {movie_id}"}
    
    async def tool_book_ticket_async(self, movie_id: int, show_time: str, num_tickets: int, customer_email: str) -> Dict[str, Any]:
        """Book movie tickets for a specific showing (asynchronous with payment processing).
        
        Args:
            movie_id: ID of the movie to book
            show_time: Show time (e.g., '14:30')  
            num_tickets: Number of tickets to book
            customer_email: Customer email for booking confirmation
                
        Returns:
            Booking confirmation dictionary
        """
        try:
            # Validate inputs
            if (movie_id is None or not isinstance(movie_id, int) or movie_id <= 0 or
                show_time is None or not isinstance(show_time, str) or not show_time.strip() or
                num_tickets is None or not isinstance(num_tickets, int) or num_tickets <= 0 or
                not customer_email or "@" not in customer_email):
                return {"error": "Invalid booking parameters"}
            
            # Find the movie
            movie = next((m for m in self.movies_data if m["id"] == movie_id), None)
            if not movie:
                return {"error": f"Movie with ID {movie_id} not found"}
            
            if show_time not in movie["showTimes"]:
                return {"error": f"Show time {show_time} not available for {movie['title']}"}
            
            # Simulate checking seat availability (async database call)
            self.logger.info("Checking seat availability for %s at %s", movie["title"], show_time)
            await sleep(0.4)
            
            # Simulate payment processing (async payment gateway call)
            self.logger.info("Processing payment for %d tickets", num_tickets)
            total_price = num_tickets * movie["price"]
            await sleep(0.6)  # Payment processing delay
            
            # Generate booking confirmation
            booking_id = f"BK{int(time())}{movie_id}"
            booking_details = {
                "booking_id": booking_id,
                "movie_id": movie_id,
                "movie_title": movie["title"],
                "show_time": show_time,
                "num_tickets": num_tickets,
                "total_price": total_price,
                "customer_email": customer_email,
                "booking_time": time(),
                "status": "confirmed",
                "async": True
            }
            
            # Store booking (simulate database save)
            await sleep(0.2)
            self.bookings[booking_id] = booking_details
            
            # Simulate sending confirmation email
            self.logger.info("Sending confirmation email to %s", customer_email)
            await sleep(0.3)
            
            return booking_details
            
        except Exception as e:
            self.logger.error("Error booking ticket: %s", e)
            return {"error": f"Booking failed: {str(e)}"}
    
    async def tool_get_booking_async(self, booking_id: str) -> Dict[str, Any]:
        """Retrieve booking details by booking ID (asynchronous).
        
        Args:
            booking_id: Booking ID to look up
            
        Returns:
            Booking details if found
        """
        try:
            # Simulate database lookup
            await sleep(0.2)
            
            if booking_id in self.bookings:
                booking = self.bookings[booking_id].copy()
                booking["retrieved_at"] = time()
                return booking
            else:
                return {"error": f"Booking {booking_id} not found"}
                
        except Exception as e:
            self.logger.error("Error retrieving booking %s: %s", booking_id, e)
            return {"error": f"Failed to retrieve booking: {str(e)}"}
    
    async def tool_search_movies_async(self, query: str) -> Dict[str, Any]:
        """Search for movies by title (asynchronous with multiple API calls).
        
        Args:
            query: Search query for movie titles
            
        Returns:
            List of matching movies
        """
        try:
            if not query or not query.strip():
                return {"error": "Search query cannot be empty"}
            
            query = query.lower().strip()
            
            # Simulate multiple concurrent API calls for comprehensive search
            search_tasks = [
                self._search_local_movies(query),
                self._search_external_api(query),
                self._search_recommendations(query)
            ]
            
            # Run searches concurrently
            local_results, external_results, recommendations = await gather(*search_tasks)
            
            # Combine and deduplicate results
            all_results = []
            all_results.extend(local_results)
            all_results.extend(external_results)
            
            return {
                "query": query,
                "local_matches": local_results,
                "external_matches": external_results,
                "recommendations": recommendations,
                "total_results": len(all_results),
                "search_time": time(),
                "async": True
            }
            
        except Exception as e:
            self.logger.error("Error searching movies for '%s': %s", query, e)
            return {"error": f"Search failed: {str(e)}"}
    
    async def _search_local_movies(self, query: str) -> List[Dict[str, Any]]:
        """Search local movie database."""
        await sleep(0.1)  # Simulate database query
        matches = [movie for movie in self.movies_data if query in movie["title"].lower()]
        return matches
    
    async def _search_external_api(self, query: str) -> List[Dict[str, Any]]:
        """Simulate external movie API search."""
        await sleep(0.8)  # Simulate slower external API
        # Return mock external results
        return [
            {"id": "ext_1", "title": f"External Movie: {query.title()}", "source": "external_api"},
            {"id": "ext_2", "title": f"Similar to {query.title()}", "source": "external_api"}
        ]
    
    async def _search_recommendations(self, query: str) -> List[Dict[str, Any]]:
        """Simulate recommendation engine."""
        await sleep(0.5)  # Simulate ML model inference
        return [
            {"id": "rec_1", "title": f"Recommended: {query.title()} 2", "source": "recommendations"},
            {"id": "rec_2", "title": f"You might like: {query.title()} Redux", "source": "recommendations"}
        ]


if __name__ == "__main__":
    # Start the Async Movie MCP server
    server = AsyncMovieMCPServer()
    server.run()
