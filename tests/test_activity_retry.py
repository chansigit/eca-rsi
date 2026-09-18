"""Poll activities outlive a shared-filesystem stall; host steps keep the short retry."""
from ecarsi.control.coordinator import activity_retry, agent_step, check_bridge, check_pool


def test_polls_retry_longer_than_host_steps():
    assert activity_retry(check_pool).maximum_attempts == 40
    assert activity_retry(check_bridge).maximum_attempts == 40
    assert activity_retry(agent_step).maximum_attempts == 3
