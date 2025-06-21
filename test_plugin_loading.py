import os
import sys
import asyncio
import datetime
from core.database import PostgresManager
from core.utils import init_postgres_manager

# Configure a test database connection
postgres_url = "postgresql://postgres:TEbfiu5mNp@postgresql.postgresql:5432/deepbot"

async def main():
    print("Testing PostgreSQL initialization...")
    
    # Test the initialization function directly
    try:
        # Initialize the PostgreSQL manager
        postgres = await init_postgres_manager(postgres_url)
        print("PostgreSQL manager initialized successfully!")
        
        # Test creating a database manager directly
        db = PostgresManager(postgres_url)
        await db.init_pool()
        print("Direct database pool initialization successful!")
        
        # Create some test data
        test_data = {
            "test_session": [
                {
                    "text": "Test reminder",
                    "datetime": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "user_name": "Test User",
                    "repeat_type": "none",
                    "creator_id": "test_user",
                    "creator_name": "Test User",
                    "is_task": False
                }
            ]
        }
        
        # Test saving and loading data
        success = await db.save_reminder_data(test_data)
        print(f"Save data result: {success}")
        
        loaded_data = await db.load_reminder_data()
        print(f"Loaded data: {loaded_data}")
        
        # Close connections
        await db.close_pool()
        print("Test completed successfully!")
        
    except Exception as e:
        print(f"Error during testing: {str(e)}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    # Run the async main function
    asyncio.run(main()) 