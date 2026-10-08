import os

import pytest

# Strict mode (ecarsi.degraded): a step that would only degrade a real run fails its test. Set at import,
# so subprocesses and pool tasks the tests start inherit it; a test of the lenient path deletes it.
os.environ["ECARSI_STRICT"] = "1"

# Test modules that need packages the control lock lacks (numpy, pandas, anndata, matplotlib, the kernels; PyYAML).
# GitHub CI (.github/workflows/tests.yml) sets ECA_TESTS=control and leaves them out; the Sherlock suite (ops/runsci.sh)
# runs everything (#49). A new module that needs these packages goes here, or CI fails at its import.
SCIENCE_ONLY = [
    "harness_bridge/test_deepseek_harness.py",
    "harness_bridge/test_mcp_server_lifecycle.py",
    "msp/test_annotation_batch_guard.py",
    "msp/test_api.py",
    "msp/test_batch_safeguard.py",
    "msp/test_cell_outliers_index_name.py",
    "msp/test_compute.py",
    "msp/test_deg_expressed.py",
    "msp/test_deg_thread_isolation.py",
    "msp/test_evidence_contracts.py",
    "msp/test_gene_summary.py",
    "msp/test_hvg_small_batches.py",
    "msp/test_integrate_end_to_end.py",
    "msp/test_integrate_statistics.py",
    "msp/test_maintenance_contracts.py",
    "msp/test_regressions.py",
    "msp/test_report_reassignment.py",
    "msp/test_report_sections.py",
    "msp/test_resources.py",
    "msp/test_scheduled_operations.py",
    "msp/test_step_recovery.py",
    "msp/test_wilcoxon_parity.py",
    "osp/test_annotation_contract.py",
    "osp/test_api.py",
    "osp/test_cluster_contracts.py",
    "osp/test_decontx_kernels.py",
    "osp/test_io.py",
    "osp/test_qc_and_decontx.py",
    "osp/test_report_contract.py",
    "standissect_lite/test_core.py",
    "test_agent_evidence.py",
    "test_agent_restart_and_skip.py",
    "test_batch_source.py",
    "test_cell_policies.py",
    "test_crosssample_v2.py",
    "test_dataset_release.py",
    "test_degraded.py",
    "test_empty_samples.py",
    "test_fragment_qc.py",
    "test_front_integration.py",
    "test_genesets.py",
    "test_organize_v2_contract.py",
    "test_osp_worker.py",
    "test_persample_v2.py",
    "test_release_links.py",
    "test_release_review_categories.py",
    "test_release_review_sample_excluded.py",
    "test_sample_map_derive.py",
    "test_sample_map_spec.py",
    "test_stress_policy.py",
    "test_tool_imports.py",
    "test_top.py",
    "test_zoomin_reassign_guard.py",
    "test_zoomin_v2.py",
    "zmip/test_api.py",
    "zmip/test_pipeline_guards.py",
    "zmip/test_publication_runtime.py",
    "zmip/test_regressions.py",
    "zmip/test_resume_and_runtime.py",
    "zmip/test_scheduled.py",
]
if os.environ.get("ECA_TESTS") == "control":
    collect_ignore = SCIENCE_ONLY


@pytest.fixture(autouse=True)
def _restore_environment():
    """Model turns run in-process in some tests, and a model turn configures its process environment
    (agent.dispatch.configure: AGENT_MODEL_POOL, the provider URL). A runner or pool task owns its
    process, so that is right in production; here it would leak into every later test."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)
