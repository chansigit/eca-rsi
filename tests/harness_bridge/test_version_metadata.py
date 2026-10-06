"""harness_bridge ships inside eca-rsi (decision 0018): its version is the last agent-harness-bridge release."""
import harness_bridge


def test_the_bridge_keeps_its_last_release_number():
    assert harness_bridge.__version__ == "0.2.15"
