"""
SchnuBot.ai - API Utilities
Gemeinsame HTTP-Retry-Logik für alle API-Calls.
"""

import time
import logging
import requests

logger = logging.getLogger(__name__)

DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE = 2  # Sekunden


def api_call_with_retry(url, headers, json_payload, timeout=120, max_retries=DEFAULT_MAX_RETRIES):
    """
    POST-Request mit exponential Backoff Retry.
    
    Returns: response.json() oder None bei Fehler.
    """
    for attempt in range(max_retries):
        try:
            response = requests.post(
                url,
                headers=headers,
                json=json_payload,
                timeout=timeout,
            )
            response.raise_for_status()
            return response.json()

        except requests.exceptions.Timeout:
            wait = DEFAULT_BACKOFF_BASE ** attempt
            logger.warning(f"API Timeout (Versuch {attempt + 1}/{max_retries}), warte {wait}s...")
            if attempt < max_retries - 1:
                time.sleep(wait)
            else:
                logger.error(f"API Timeout nach {max_retries} Versuchen")
                return None

        except requests.exceptions.ConnectionError:
            wait = DEFAULT_BACKOFF_BASE ** attempt
            logger.warning(f"API Connection Error (Versuch {attempt + 1}/{max_retries}), warte {wait}s...")
            if attempt < max_retries - 1:
                time.sleep(wait)
            else:
                logger.error(f"API nicht erreichbar nach {max_retries} Versuchen")
                return None

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response else 0
            if status >= 500:
                wait = DEFAULT_BACKOFF_BASE ** attempt
                logger.warning(f"API Server Error {status} (Versuch {attempt + 1}/{max_retries}), warte {wait}s...")
                if attempt < max_retries - 1:
                    time.sleep(wait)
                else:
                    logger.error(f"API Server Error nach {max_retries} Versuchen")
                    return None
            else:
                logger.error(f"API Client Error {status}: {e}")
                return None

        except requests.exceptions.RequestException as e:
            logger.error(f"API Fehler: {e}")
            return None

    return None
