import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fetchWorkflowLinkedProjects } from "../shared/project-context";
import { fetchProjectScheduledJobs } from "@/pages/projects/scheduled-jobs/api";
import { CanvasScheduleSummary } from "./canvas-schedule-summary";
vi.mock("../shared/project-context",()=>({fetchWorkflowLinkedProjects:vi.fn()}));
vi.mock("@/pages/projects/scheduled-jobs/api",()=>({fetchProjectScheduledJobs:vi.fn()}));
function renderSummary(projectHint="") { return render(<QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}><CanvasScheduleSummary workflowId="safe" projectHint={projectHint}/></QueryClientProvider>); }
beforeEach(()=>vi.clearAllMocks());
it("uses the selected accessible project's actual schedules, timezone and status without input values", async()=>{
 vi.mocked(fetchWorkflowLinkedProjects).mockResolvedValue([{id:"one",name:"One"},{id:"two",name:"Two"}]);
 vi.mocked(fetchProjectScheduledJobs).mockResolvedValue({project_id:"two",timezone_context:{},runs:[],jobs:[{id:"a",workflow_id:"safe",cron_expr:"0 9 * * *",schedule_timezone:"Europe/Berlin",enabled:false,inputs:{token:"NEVER_RENDER"}},{id:"b",workflow_id:"other",cron_expr:"OTHER_SCHEDULE"}]});
 renderSummary("two");
 expect(await screen.findByText(/0 9 \* \* \* · Europe\/Berlin · Paused/)).toBeVisible();
 expect(fetchProjectScheduledJobs).toHaveBeenCalledWith("two");
 expect(document.body).not.toHaveTextContent(/NEVER_RENDER|OTHER_SCHEDULE/);
});
it("does not guess a project when multiple projects are linked", async()=>{
 vi.mocked(fetchWorkflowLinkedProjects).mockResolvedValue([{id:"one",name:"One"},{id:"two",name:"Two"}]);
 renderSummary();
 expect(await screen.findByText(/Choose a linked project in Schedules/)).toBeVisible();
 expect(fetchProjectScheduledJobs).not.toHaveBeenCalled();
});
it("keeps unauthorized project hints out of schedule reads", async()=>{
 vi.mocked(fetchWorkflowLinkedProjects).mockResolvedValue([{id:"one",name:"One"}]);
 renderSummary("denied");
 expect(await screen.findByText(/Choose a linked project in Schedules/)).toBeVisible();
 expect(fetchProjectScheduledJobs).not.toHaveBeenCalled();
});
