"""
LangGraph Parallel Sweep
=========================

StateGraph that fans out sweep phases to run concurrently on different
Ollama instances from the pool.

    START → fan_out_sweeps → [metrics_sweep, logs_sweep, containers_sweep] → merge_findings → END

Each node checks out an Ollama instance from the pool, runs the sweep
with that instance's URL/model, and checks the instance back in.
"""

import logging
import operator
import time
from typing import Annotated, Any, TypedDict

from langgraph.constants import Send
from langgraph.graph import StateGraph, END

from ollama_pool import OllamaPool, SWEEP_DURATION, SWEEP_PHASE_DURATION

logger = logging.getLogger("cfoperator.sweep_graph")

# Sweep phase definitions — task prompts matching existing _sweep_metrics/_sweep_logs
SWEEP_PHASES = {
    'metrics': {
        'config_key': 'metrics',
        'task': (
            "Check the health of all infrastructure hosts and services by examining metrics. "
            "Look at resource usage, scrape targets, pod/container health, and anything that looks off. "
            "Use k8s tools (k8s_get_pods, k8s_get_nodes, k8s_get_events) for Kubernetes workloads "
            "and prometheus_query for host-level metrics."
        ),
    },
    'logs': {
        'config_key': 'logs',
        'task': (
            "Check recent logs across infrastructure services for errors, warnings, or concerning patterns. "
            "Use loki_query with correct LogQL syntax. "
            "CORRECT examples: "
            "{namespace=\"apps\"} |= \"error\" -- "
            "{namespace=~\"apps|monitoring\"} |~ \"error|warning\" -- "
            "{pod=~\"cfoperator.*\"} |= \"error\" -- "
            "{namespace=\"monitoring\", container=\"prometheus\"} |= \"error\". "
            "Use =~ for multi-value matching. NEVER use || between {} selectors."
        ),
    },
    'containers': {
        'config_key': 'containers',
        'task': (
            "Review workload health across the fleet — Kubernetes pods, bare-metal services, "
            "and any Docker containers. Use k8s_get_pods, k8s_get_all_unhealthy, and k8s_get_events for k8s workloads across apps, monitoring, data, iot, ai, infrastructure, and kube-system, "
            "loki_query for workload logs, prometheus_query for resource usage, and ssh_list_services for bare-metal hosts. "
            "Do not rely only on current pod phase: recovered failures may appear only in recent Kubernetes warning events or Loki logs. "
            "Check for BackOff, Unhealthy/readiness failures, restarts, CrashLoopBackOff history, and other issues."
        ),
    },
}


class SweepState(TypedDict):
    """State for the parallel sweep graph."""
    findings: Annotated[list, operator.add]
    pool: Any
    sweep_config: dict
    agent_ref: Any
    start_time: float
    phase_name: str
    phase_task: str


def fan_out_sweeps(state: dict) -> list[Send]:
    """Dispatch enabled sweep phases as parallel nodes."""
    sweep_config = state['sweep_config']
    sends = []
    for phase_name, phase_def in SWEEP_PHASES.items():
        config_key = phase_def['config_key']
        if sweep_config.get(config_key):
            sends.append(Send("run_sweep_phase", {
                **state,
                'phase_name': phase_name,
                'phase_task': phase_def['task'],
            }))
    logger.info(f"Fan-out: dispatching {len(sends)} sweep phases")
    return sends


def run_sweep_phase(state: dict) -> dict:
    """
    Execute a single sweep phase on a checked-out Ollama instance.

    Checks out an instance from the pool, runs the LLM sweep, checks it back in.
    Falls back to the agent's default provider if no pool instance is available.
    """
    pool: OllamaPool = state['pool']
    agent = state['agent_ref']
    phase_name = state['phase_name']
    phase_task = state['phase_task']

    phase_start = time.time()
    instance = pool.checkout()

    if instance:
        try:
            logger.info(f"Phase '{phase_name}' running on {instance.name} ({instance.model})")
            findings = agent._sweep_with_llm_on_instance(
                task=phase_task,
                url=instance.url,
                model=instance.model,
            )
            duration = time.time() - phase_start
            SWEEP_PHASE_DURATION.labels(phase=phase_name, instance=instance.name).observe(duration)
            logger.info(f"Phase '{phase_name}' on {instance.name}: {len(findings)} findings in {duration:.1f}s")
            return {'findings': findings}
        except Exception as e:
            logger.error(f"Phase '{phase_name}' failed on {instance.name}: {e}")
            return {'findings': []}
        finally:
            pool.checkin(instance)
    else:
        # No pool instance available — fallback to default provider
        logger.warning(f"Phase '{phase_name}': no pool instance available, using default provider")
        try:
            findings = agent._sweep_with_llm(phase_task)
            duration = time.time() - phase_start
            SWEEP_PHASE_DURATION.labels(phase=phase_name, instance='default').observe(duration)
            return {'findings': findings}
        except Exception as e:
            logger.error(f"Phase '{phase_name}' fallback failed: {e}")
            return {'findings': []}


def merge_findings(state: dict) -> dict:
    """Deduplicate and merge findings from all phases."""
    agent = state['agent_ref']
    findings = state.get('findings', [])
    start_time = state.get('start_time', time.time())

    deduped = agent._dedup_findings(findings)
    duration = time.time() - start_time

    # Count unique instances used (from phase durations logged above)
    logger.info(f"Parallel sweep completed: {len(deduped)} findings (from {len(findings)} raw) in {duration:.1f}s")
    SWEEP_DURATION.labels(mode='parallel').observe(duration)

    return {'findings': deduped}


def build_sweep_graph() -> StateGraph:
    """Build the LangGraph StateGraph for parallel sweeps."""
    graph = StateGraph(SweepState)

    graph.add_node("run_sweep_phase", run_sweep_phase)
    graph.add_node("merge_findings", merge_findings)

    graph.set_conditional_entry_point(fan_out_sweeps)
    graph.add_edge("run_sweep_phase", "merge_findings")
    graph.add_edge("merge_findings", END)

    return graph.compile()


# Module-level compiled graph (reusable)
_sweep_graph = None


def get_sweep_graph():
    """Get or build the compiled sweep graph (lazy singleton)."""
    global _sweep_graph
    if _sweep_graph is None:
        _sweep_graph = build_sweep_graph()
    return _sweep_graph


def run_parallel_sweep(agent, pool: OllamaPool, sweep_config: dict) -> list[dict]:
    """
    Execute parallel sweep phases using the LangGraph graph.

    Args:
        agent: CFOperator instance (provides _sweep_with_llm_on_instance, _dedup_findings)
        pool: OllamaPool with available instances
        sweep_config: OODA sweep config dict (metrics: true, logs: true, etc.)

    Returns:
        List of deduplicated findings
    """
    graph = get_sweep_graph()

    initial_state = {
        'findings': [],
        'pool': pool,
        'sweep_config': sweep_config,
        'agent_ref': agent,
        'start_time': time.time(),
    }

    result = graph.invoke(initial_state)
    return result.get('findings', [])
