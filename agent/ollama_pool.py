"""
Ollama Pool Manager
====================

Manages a pool of Ollama GPU instances for parallel sweep execution.
Each instance runs on a separate GPU — checkout/checkin ensures exclusive access
(single GPU = one inference at a time).

Thread-safe via threading.Lock (sweeps run in background threads).
"""

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import requests
from prometheus_client import Counter, Gauge, Histogram

logger = logging.getLogger("cfoperator.pool")

# Prometheus metrics — pool-level
POOL_INSTANCES = Gauge(
    'cfoperator_pool_instances', 'Pool instance status',
    ['instance', 'status']  # status: healthy/unhealthy/in_use
)
POOL_CHECKOUTS = Counter(
    'cfoperator_pool_checkouts_total', 'Pool checkout attempts',
    ['instance', 'result']  # result: success/unavailable
)
POOL_CHECKINS = Counter(
    'cfoperator_pool_checkins_total', 'Pool checkins', ['instance']
)
POOL_HEALTH_CHECKS = Counter(
    'cfoperator_pool_health_checks_total', 'Health check results',
    ['instance', 'result']  # result: healthy/unreachable
)

# Sweep-level timing
SWEEP_DURATION = Histogram(
    'cfoperator_sweep_duration_seconds', 'Total sweep duration',
    ['mode']  # mode: parallel/sequential
)
SWEEP_PHASE_DURATION = Histogram(
    'cfoperator_sweep_phase_duration_seconds', 'Per-phase sweep duration',
    ['phase', 'instance']
)


class OllamaInstance:
    """A single Ollama GPU instance in the pool."""

    def __init__(self, name: str, url: str, model: str, enabled: bool = True):
        self.name = name
        self.url = url.rstrip('/')
        self.model = model
        self.models: list[str] = []  # discovered via /api/tags
        self.in_use = False
        self.healthy = True
        self.enabled = enabled
        self.last_checkout: Optional[str] = None
        self.last_health_check: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            'name': self.name,
            'url': self.url,
            'model': self.model,
            'models': self.models,
            'healthy': self.healthy,
            'in_use': self.in_use,
            'enabled': self.enabled,
            'last_checkout': self.last_checkout,
        }


class OllamaPool:
    """
    Pool of Ollama instances with checkout/checkin for exclusive access.

    Usage:
        pool = OllamaPool(instances_config)
        inst = pool.checkout(preferred_model='qwen3:14b')
        try:
            # use inst.url and inst.model for LLM calls
            ...
        finally:
            pool.checkin(inst)
    """

    HEALTH_CHECK_INTERVAL = 300  # 5 minutes

    def __init__(self, instances: list[dict], kb=None):
        self._lock = threading.Lock()
        self._instances: list[OllamaInstance] = []
        self._kb = kb  # KnowledgeBase for persisting enabled/disabled state

        # Load persisted enabled state from DB
        disabled = set()
        if kb:
            try:
                raw = kb.get_setting('ollama_pool_disabled', '')
                if raw:
                    disabled = set(raw.split(','))
            except Exception:
                pass

        for cfg in instances:
            name = cfg['name']
            inst = OllamaInstance(
                name=name,
                url=cfg['url'],
                model=cfg.get('model', ''),
                enabled=name not in disabled,
            )
            self._instances.append(inst)

        enabled_names = [i.name for i in self._instances if i.enabled]
        disabled_names = [i.name for i in self._instances if not i.enabled]
        logger.info(f"Ollama pool initialized with {len(self._instances)} instances: "
                     f"enabled={enabled_names}, disabled={disabled_names}")

        # Run initial model discovery in background
        self._discover_thread = threading.Thread(
            target=self.discover_models, daemon=True
        )
        self._discover_thread.start()

        # Start periodic health check thread
        self._health_thread = threading.Thread(
            target=self._health_check_loop, daemon=True
        )
        self._health_thread.start()

    def discover_models(self):
        """Query /api/tags on each instance to discover available models."""
        for inst in self._instances:
            try:
                resp = requests.get(f"{inst.url}/api/tags", timeout=5)
                resp.raise_for_status()
                data = resp.json()
                inst.models = [m['name'] for m in data.get('models', [])]
                inst.healthy = True
                inst.last_health_check = time.time()
                POOL_HEALTH_CHECKS.labels(instance=inst.name, result='healthy').inc()
                POOL_INSTANCES.labels(instance=inst.name, status='healthy').set(1)
                POOL_INSTANCES.labels(instance=inst.name, status='unhealthy').set(0)
                logger.info(f"Pool discovery: {inst.name} has {len(inst.models)} models: "
                             f"{inst.models[:5]}")
            except Exception as e:
                inst.healthy = False
                inst.last_health_check = time.time()
                POOL_HEALTH_CHECKS.labels(instance=inst.name, result='unreachable').inc()
                POOL_INSTANCES.labels(instance=inst.name, status='healthy').set(0)
                POOL_INSTANCES.labels(instance=inst.name, status='unhealthy').set(1)
                logger.warning(f"Pool instance unhealthy: {inst.name} ({inst.url}): {e}")

    def _health_check_loop(self):
        """Periodically re-check instance health."""
        while True:
            time.sleep(self.HEALTH_CHECK_INTERVAL)
            try:
                self.discover_models()
            except Exception as e:
                logger.error(f"Pool health check loop error: {e}")

    def checkout(self, preferred_model: str = None) -> Optional[OllamaInstance]:
        """
        Check out an available instance from the pool.

        Prefers an instance that has the preferred_model available.
        Returns None if no instance is free. Skips disabled instances.
        """
        with self._lock:
            # First pass: find a free, healthy, enabled instance with the preferred model
            if preferred_model:
                for inst in self._instances:
                    if not inst.in_use and inst.healthy and inst.enabled:
                        if preferred_model in inst.models or inst.model == preferred_model:
                            inst.in_use = True
                            inst.last_checkout = datetime.now(timezone.utc).isoformat()
                            POOL_CHECKOUTS.labels(instance=inst.name, result='success').inc()
                            POOL_INSTANCES.labels(instance=inst.name, status='in_use').set(1)
                            logger.info(f"Pool checkout: {inst.name} (preferred model: {preferred_model})")
                            return inst

            # Second pass: any free, healthy, enabled instance
            for inst in self._instances:
                if not inst.in_use and inst.healthy and inst.enabled:
                    inst.in_use = True
                    inst.last_checkout = datetime.now(timezone.utc).isoformat()
                    POOL_CHECKOUTS.labels(instance=inst.name, result='success').inc()
                    POOL_INSTANCES.labels(instance=inst.name, status='in_use').set(1)
                    logger.info(f"Pool checkout: {inst.name} (any available)")
                    return inst

            # No instance available
            POOL_CHECKOUTS.labels(instance='none', result='unavailable').inc()
            logger.debug("Pool checkout: no instance available")
            return None

    def checkin(self, instance: OllamaInstance):
        """Return an instance to the pool."""
        with self._lock:
            instance.in_use = False
            POOL_CHECKINS.labels(instance=instance.name).inc()
            POOL_INSTANCES.labels(instance=instance.name, status='in_use').set(0)
            logger.info(f"Pool checkin: {instance.name}")

    def available_count(self) -> int:
        """Number of healthy, enabled, non-in-use instances."""
        with self._lock:
            return sum(1 for i in self._instances if not i.in_use and i.healthy and i.enabled)

    def set_enabled(self, instance_name: str, enabled: bool) -> bool:
        """Enable or disable a pool instance. Persists to DB."""
        with self._lock:
            for inst in self._instances:
                if inst.name == instance_name:
                    inst.enabled = enabled
                    logger.info(f"Pool instance {instance_name} {'enabled' if enabled else 'disabled'}")
                    # Persist disabled list to DB
                    disabled = [i.name for i in self._instances if not i.enabled]
                    if self._kb:
                        try:
                            self._kb._kb.set_setting('ollama_pool_disabled', ','.join(disabled))
                        except Exception as e:
                            logger.warning(f"Could not persist pool state: {e}")
                    return True
            return False

    def status(self) -> dict:
        """Return pool status for the web API."""
        with self._lock:
            instances = [inst.to_dict() for inst in self._instances]
            enabled = sum(1 for i in self._instances if i.enabled)
            healthy = sum(1 for i in self._instances if i.healthy and i.enabled)
            available = sum(1 for i in self._instances if not i.in_use and i.healthy and i.enabled)
            mode = 'parallel' if available >= 2 else 'sequential'
            return {
                'instances': instances,
                'total': len(self._instances),
                'enabled': enabled,
                'healthy': healthy,
                'available': available,
                'mode': mode,
            }
