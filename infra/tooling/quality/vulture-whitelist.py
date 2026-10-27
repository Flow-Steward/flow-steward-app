# Vulture false positives that preserve runtime-compatible call signatures.

capture_output  # unused variable (core/application/release/service.py:55)
run_manager  # unused variable (core/application/orchestration/extension_chat_model.py:126)
run_config_override  # keyword-only Protocol contract (service_bindings/contracts.py:154)
resume_cancelled  # keyword-only Protocol contract (runtime/graph_resume_wiring.py:99)
defer_terminal_persistence  # keyword-only Protocol contract (runtime/graph_resume_wiring.py:100)
