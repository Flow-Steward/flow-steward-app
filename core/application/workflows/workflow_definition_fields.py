"""Field vocabulary shared by definition normalization and change operations.

The values stay independent of change execution and its error types.
"""

#: The skill is not among them: it is always derived from the step's agent, so a
#: definition never carries a stored one that could outlive an agent change.
STEP_FIELDS = (
    "step_id",
    "step_kind",
    "group_id",
    "for_each_id",
    "lane_id",
    "block_key",
    "display_name",
    "participant_key",
    "agent_id",
    "connector_id",
    "connector_install_id",
    "workflow_id",
    "input_mapping_jsonb",
    "output_mapping_jsonb",
    "connection_binding_jsonb",
    "error_policy",
    "boundary_contract",
    "optional",
    "disabled",
    "context_inject",
    "override",
)
#: What a kind change keeps; everything else is the old kind's configuration.
STEP_KIND_INDEPENDENT_FIELDS = (
    "step_id",
    "group_id",
    "for_each_id",
    "lane_id",
    "display_name",
    "error_policy",
    "boundary_contract",
    "optional",
    "disabled",
)
GROUP_FIELDS = (
    "group_id",
    "display_name",
    "description",
    "body",
    "error_policy",
    "boundary_contract",
)
#: Fields of the pre-release loop group. They are carried to the validator,
#: which refuses them, instead of being dropped silently (ADR 0038).
REMOVED_GROUP_FIELDS = ("kind", "loop")
FOR_EACH_FIELDS = (
    "for_each_id",
    "group_id",
    "display_name",
    "description",
    "over",
    "item_name",
    "index_name",
    "body",
    "error_policy",
    "boundary_contract",
)
