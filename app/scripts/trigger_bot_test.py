import os
import pandas as pd
from datetime import datetime
import pytz
from celery import Celery
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure Celery app (match your project's settings)
celery_app = Celery(
    'tasks',
    broker=os.getenv('CELERY_BROKER_URL', 'redis://localhost:6379/0'),
    backend=os.getenv('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0')
)

# Mock data for testing
def get_mock_data():
    data = {
        'timestamp': [datetime(2025, 8, 1, 15, 55, tzinfo=pytz.UTC).astimezone(pytz.timezone('US/Eastern'))],
        'open': [300.0], 'high': [305.0], 'low': [298.0], 'close': [302.0], 'volume': [1000],
        'SMI': [-75], 'SMI_Signal': [-70], 'SMI_Change': [5], 'Buy_Signal': [True], 'Sell_Signal': [False]
    }
    return pd.DataFrame(data).set_index('timestamp')

def trigger_bot_test(bot_id):
    # For simplicity, we're not passing the DataFrame directly (Celery tasks need to handle data fetch internally).
    # Instead, we trigger the task and let it use its own logic or mock data.
    result = celery_app.send_task('app.tasks.run_paper_bot_tick', args=[bot_id])
    print(f"Task sent with ID: {result.id}")
    return result

if __name__ == "__main__":
    # Example usage
    bot_id = 60  # Replace with your test bot_id
    print(f"Triggering bot test for bot_id={bot_id}...")
    result = trigger_bot_test(bot_id)
    # Optionally wait for result (if async result is needed)
    # result.get(timeout=10)  # Uncomment if you want to wait and get the result