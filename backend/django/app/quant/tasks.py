# backend/django/app/quant/tasks.py

from celery import shared_task
import logging
from celery.exceptions import SoftTimeLimitExceeded

from app.quant.algorithms.mean_reversion.entry import entry_algorithm
from app.quant.algorithms.mean_reversion.trailing import trailing_stop_algorithm
from app.quant.algorithms.close.close import close_algorithm

logger = logging.getLogger(__name__)

@shared_task(name='quant.tasks.run_quant_entry_algorithm', max_retries=3, soft_time_limit=30)
def run_quant_entry_algorithm():
    try:
        logger.info("Starting quant entry algorithm...")
        entry_algorithm()
    except SoftTimeLimitExceeded:
        logger.error("Task timed out.")
    except Exception as e:
        logger.error(f"Error in quant entry algorithm: {e}")

@shared_task(name='quant.tasks.run_quant_trailing_stop_algorithm', max_retries=3, soft_time_limit=30)
def run_quant_trailing_stop_algorithm():
    try:
        logger.info("Starting quant trailing stop algorithm...")
        trailing_stop_algorithm()
    except SoftTimeLimitExceeded:
        logger.error("Task timed out during trailing stop algorithm.")
    except Exception as e:
        logger.error(f"Error in quant trailing stop algorithm: {e}")

@shared_task(name='quant.tasks.run_quant_close_algorithm', max_retries=3, soft_time_limit=30)
def run_quant_close_algorithm():
    try:
        logger.info("Starting quant close algorithm...")
        close_algorithm()
    except SoftTimeLimitExceeded:
        logger.error("Task timed out.")
    except Exception as e:
        logger.error(f"Error in quant close algorithm: {e}")

