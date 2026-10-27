/**
 * Which dialog field a rejected notification route is about.
 *
 * The message alone used to be the whole answer, and the editor rendered it on
 * the card *behind* the modal overlay — so on screen an empty submit looked like
 * a dead button. Naming the field lets the dialog mark the control, say why in
 * front of the operator, and move focus there.
 */

export type RouteFieldId = "route_key" | "provider_key" | "event_type" | "channel_id";

/** The control that owns each field, so focus can be moved to it after a refusal. */
export const ROUTE_FIELD_ELEMENT_IDS: Record<RouteFieldId, string> = {
  route_key: "notification-route-key",
  provider_key: "notification-route-provider",
  event_type: "notification-route-event-type",
  channel_id: "notification-route-target",
};

export class RouteFieldError extends Error {
  readonly field: RouteFieldId;

  constructor(field: RouteFieldId, message: string) {
    super(message);
    this.name = "RouteFieldError";
    this.field = field;
  }
}
