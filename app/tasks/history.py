# app/tasks/history.py
from celery import shared_task
from app.scripts.export_history import export_history

@shared_task
def update_history_csv(user_id: int = 116):
    """
    Celery task that pulls option + stock bot trade history
    for the given user and writes it to a CSV file.
    """
    try:
        export_history(user_id=user_id)
        return f"✅ History CSV updated for user {user_id}"
    except Exception as e:
        return f"❌ Failed to update history CSV: {e}"
