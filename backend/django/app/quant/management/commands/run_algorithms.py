# backend/django/app/quant/management/commands/run_algorithms.py

from django.core.management.base import BaseCommand
from app.quant.algorithms.mean_reversion.entry import entry_algorithm
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Runs the quant algorithms continuously.'

    def handle(self, *args, **options):
        logger.info("Starting quant entry algorithm...")
        try:
            entry_algorithm()
        except KeyboardInterrupt:
            logger.info("Quant entry algorithm stopped manually.")
        except Exception as e:
            logger.error(f"Unhandled exception: {e}", exc_info=True)