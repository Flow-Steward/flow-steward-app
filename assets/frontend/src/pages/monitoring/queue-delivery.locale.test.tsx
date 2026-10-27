import { MemoryRouter } from "react-router";
import { render, screen } from "@testing-library/react";
import { I18nContext, fallbackContext } from "@/lib/i18n/context";
import { QueueDeliveryCard } from "./monitoring-display";

const translations: Record<string, string> = {
  "react.monitoring.delivery_idle_dead_lettered_message": "Aktyvaus pristatymo užduočių eilės šiuo metu nėra, tačiau bent vienai nepristatytai užduočiai reikia operatoriaus įsikišimo.",
  "react.monitoring.delivery_idle_execution_failed_message": "Aktyvaus pristatymo užduočių eilės šiuo metu nėra, tačiau bent viena užduotis anksčiau nepavyko po sėkmingo pristatymo.",
  "react.monitoring.delivery_queued_pending_label": "Įprastai eilėje / laukiama",
  "react.monitoring.delivery_queued_pending_message": "Užduotys įprastai laukia eilėje, kol jas paims vykdytojas.",
  "react.monitoring.state_idle": "Laukia",
  "react.monitoring.delivery_retrying_delivery_label": "Pristatymas kartojamas",
  "react.monitoring.delivery_retrying_delivery_message": "RabbitMQ pristatymas kartojamas prieš saugiai pradedant vykdymą.",
  "react.monitoring.delivery_dead_lettered_label": "Pristatymas nutrauktas / reikalingas įsikišimas",
  "react.monitoring.delivery_operator_action": "Atidarykite nurodytą užduotį ir ištaisykite problemą.",
  "react.monitoring.delivery_idle_message": "Šiuo metu šioje srityje nėra laukiančių eilėje pristatymo užduočių.",
};
const value = {
  state: "idle", state_label: "Idle", state_label_key: "react.monitoring.state_idle",
  state_message: "No queued delivery work is waiting for this scope right now.",
  state_message_key: "react.monitoring.delivery_idle_message",
  queued_pending_jobs: 0, retrying_delivery_jobs: 0, dead_lettered_jobs: 0,
  post_claim_execution_failed_jobs: 0, attention_jobs: [], requires_intervention: false,
};
function show(payload: Record<string, unknown>, locale = "lt") {
  render(<I18nContext.Provider value={{ ...fallbackContext, locale, t: (key, text, params) => fallbackContext.t(key, locale === "lt" ? translations[key] || text : text, params) }}><MemoryRouter><QueueDeliveryCard value={payload} /></MemoryRouter></I18nContext.Provider>);
}
describe("Queue delivery localized copy", () => {
  it.each(["react.monitoring.delivery_idle_dead_lettered_message", "react.monitoring.delivery_idle_execution_failed_message"])("retains the specific translated attention paragraph for %s", (key) => {
    show({ ...value, state_message: "No active backlog, but one or more jobs need attention.", state_message_key: key, dead_lettered_jobs: 1 });
    expect(screen.getByText(translations[key]!)).toBeInTheDocument();
    expect(screen.queryByText(translations["react.monitoring.delivery_idle_message"]!)).not.toBeInTheDocument();
  });
  it("renders queued delivery without relabeling its machine state", () => {
    const payload = { ...value, state: "queued_pending", state_label: "Normal queued / pending", state_label_key: "react.monitoring.delivery_queued_pending_label", state_message: "Jobs are queued normally and waiting for worker pickup.", state_message_key: "react.monitoring.delivery_queued_pending_message", queued_pending_jobs: 1 };
    show(payload);
    expect(screen.getByText(translations["react.monitoring.delivery_queued_pending_label"]!)).toBeInTheDocument();
    expect(screen.getByText(translations["react.monitoring.delivery_queued_pending_message"]!)).toBeInTheDocument();
    expect(payload.state).toBe("queued_pending");
  });
  it("shows translated retry, attention and operator copy while retaining raw failure diagnostics", () => {
    const payload = { ...value, state: "retrying_delivery", state_label: "Retrying delivery", state_label_key: "react.monitoring.delivery_retrying_delivery_label", state_message: "RabbitMQ delivery is retrying before execution has safely started.", state_message_key: "react.monitoring.delivery_retrying_delivery_message", operator_action: "Open a listed job", operator_action_key: "react.monitoring.delivery_operator_action", retrying_delivery_jobs: 1, dead_lettered_jobs: 1, requires_intervention: true, attention_jobs: [{ job_id: "job-owner", attention_label: "Dead-lettered / requires intervention", attention_label_key: "react.monitoring.delivery_dead_lettered_label", failure_message: "Owner Ąžuolas failure: state.source", failure_code: "project_not_found", project_name: "Preflight" }] };
    const before = JSON.stringify(payload);
    show(payload);
    expect(screen.getByText(translations["react.monitoring.delivery_retrying_delivery_label"]!)).toBeInTheDocument();
    expect(screen.getByText(translations["react.monitoring.delivery_retrying_delivery_message"]!)).toBeInTheDocument();
    expect(screen.getByText(translations["react.monitoring.delivery_dead_lettered_label"]!)).toBeInTheDocument();
    expect(screen.getByText(translations["react.monitoring.delivery_operator_action"]!)).toBeInTheDocument();
    expect(screen.getByText("Owner Ąžuolas failure: state.source")).toBeInTheDocument();
    expect(screen.getByText("project_not_found")).toBeInTheDocument();
    expect(screen.getByText("Preflight")).toBeInTheDocument();
    expect(JSON.stringify(payload)).toBe(before);
  });
  it.each(["lt", "en"])("renders explicit owned keys in %s without mutating the payload", (locale) => {
    const before = JSON.stringify(value);
    show(value, locale);
    expect(screen.getByText(locale === "lt" ? "Laukia" : "Idle")).toBeInTheDocument();
    expect(screen.getByText(locale === "lt" ? translations["react.monitoring.delivery_idle_message"]! : value.state_message)).toBeInTheDocument();
    expect(JSON.stringify(value)).toBe(before);
  });
  it("retains an unkeyed attention diagnostic while localizing the idle label", () => {
    show({ ...value, state_message_key: "", state_message: "Owner Ąžuolas failure: state.source" });
    expect(screen.getByText("Owner Ąžuolas failure: state.source")).toBeInTheDocument();
    expect(screen.queryByText(translations["react.monitoring.delivery_idle_message"]!)).not.toBeInTheDocument();
  });
});
