"""
helix_engine.plugins — Built-in task resolvers
================================================

Plugin resolvers handle the actual execution of BPMN tasks.
Each resolver implements the ``TaskResolver`` protocol.

Built-in resolvers:
  - ``HttpTaskResolver``  — calls REST APIs for ServiceTasks
"""
