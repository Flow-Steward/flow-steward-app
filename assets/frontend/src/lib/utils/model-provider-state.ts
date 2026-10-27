import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import type { QueryClient } from "@tanstack/react-query";

function providerErrorMessages(t: I18nContextValue["t"]): Record<string, string> {
 return {
  authentication_error: t("react.extensions.manual_the_provider_rejected_the_api_key_check_that_you_copied_the_whole_key_then_save_it_again", "The provider rejected the API key. Check that you copied the whole key, then save it again."),
  billing_quota_exceeded: t("react.extensions.manual_the_provider_account_has_insufficient_quota_or_credit", "The provider account has insufficient quota or credit."),
  verification_expired: t("react.extensions.manual_verification_expired_run_verify_before_routing_this_model_again", "Verification expired. Run Verify before routing this model again."),
  capability_mismatch: t("react.extensions.manual_the_selected_model_does_not_satisfy_the_required_chat_capabilities", "The selected model does not satisfy the required chat capabilities."),
  // One per verification check, so the row says which capability failed.
  capability_mismatch_chat: t("react.extensions.manual_failed_the_chat_check_the_model_did_not_follow_a_short_exact_instruction", "Failed the chat check: the model did not follow a short exact instruction."),
  capability_mismatch_tools: t("react.extensions.manual_failed_the_tool_calling_check_the_model_did_not_call_the_test_tool_once_with_the_right_arguments", "Failed the tool-calling check: the model did not call the test tool once with the right arguments."),
  capability_mismatch_tool_result: t("react.extensions.manual_failed_the_tool_result_check_the_model_did_not_use_the_value_the_test_tool_returned", "Failed the tool-result check: the model did not use the value the test tool returned."),
  capability_mismatch_structured_output: t("react.extensions.manual_failed_the_structured_output_check_the_answer_did_not_match_the_required_json_schema", "Failed the structured-output check: the answer did not match the required JSON schema."),
  capability_mismatch_tool_result_structured: t("react.extensions.manual_failed_the_combined_check_the_model_could_not_return_a_tool_s_result_as_structured_output", "Failed the combined check: the model could not return a tool's result as structured output."),
  configuration_changed: t("react.extensions.manual_the_provider_settings_changed_verify_this_model_again_before_using_it", "The provider settings changed. Verify this model again before using it."),
  credentials_changed: t("react.extensions.manual_the_provider_key_changed_verify_this_model_again_before_using_it", "The provider key changed. Verify this model again before using it."),
  // A new release of the provider extension is new code: verification is bound
  // to the release, so the update makes verified models stale on purpose.
  extension_release_or_descriptor_changed: t("react.extensions.manual_the_extension_was_updated_verify_this_model_again_before_using_it", "The extension was updated. Verify this model again before using it."),
  invalid_response: t("react.extensions.manual_the_provider_returned_an_invalid_response", "The provider returned an invalid response."),
  model_unavailable: t("react.extensions.manual_the_selected_model_is_unavailable", "The selected model is unavailable."),
  rate_limited: t("react.extensions.manual_the_provider_is_rate_limited_try_again_later", "The provider is rate limited. Try again later."),
  runtime_endpoint_required: t("react.extensions.manual_a_public_https_runtime_endpoint_is_required", "A public HTTPS runtime endpoint is required."),
  timeout: t("react.extensions.manual_the_provider_request_timed_out_try_again_later", "The provider request timed out. Try again later."),
  upstream_error: t("react.extensions.manual_the_provider_request_failed_try_again_later", "The provider request failed. Try again later."),
 };
}

export function providerErrorMessage(value: unknown, fallback: string, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (typeof value !== "string" || !value.trim()) {
    return fallback;
  }
  const normalized = value.trim();
  return providerErrorMessages(t)[normalized] || (normalized.includes(" ") ? normalized : fallback);
}

export async function invalidateModelRoutingQueries(queryClient: QueryClient): Promise<void> {
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: ["model-selection"] }),
    queryClient.invalidateQueries({ queryKey: ["projects"] }),
  ]);
}
