"""Temporal control plane: `coordinator` (activities, AgentWorkflow, CLI), `temporal` (the service),
and the dataset / persample / crosssample / zoomin workflows. Importing the package itself needs no temporalio,
so host-side tools (the observatory) can read `control.temporal.endpoint`."""
