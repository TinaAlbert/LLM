export interface User { id: string; display_name: string; roles?: string[]; organization_id?: string; department_id?: string }
export interface Profile { id: string | null; source_ref: string | null; revision: number }
export interface Trip {
  id: string; owner_id: string; organization_id: string | null; flight_id: string; travel_date: string;
  passengers: number; revision: number; status: string; price: string | null; price_status: string;
  budget_selection?: { project_id: string; selection_revision: number } | null;
}
export interface Price {
  id: string; trip_id: string; trip_revision: number; amount_minor: number; base_amount_minor: number;
  coupon_discount_minor: number; currency: string; payment_kind: string; status: string; effective_status: string;
  valid: boolean; expires_at: string; price_basis: string; coupon_id?: string | null;
}
export interface Payment { id: string; amount_minor: number; status: string; ticket_task_id: string | null }
export interface Task { id: string; status: string; ticket_id: string | null }
export interface Ticket { id: string; status: string; certificate: string; trip_revision: number }
export interface Approval {
  id: string; trip_id: string; revision: number; trip_revision: number; applicant_name: string;
  flight_id: string; travel_date: string; passengers: number; amount_minor: number; project_id: string;
  status: string; resource_status: string; valid: boolean; effective_status: string;
}
export interface Booking { trip: Trip; quote: Price | null; lock: Price | null; payment: Payment | null; ticket_task: Task | null; ticket: Ticket | null; approval: Approval | null }
export interface Options { flights: { id: string; travel_date: string }[]; sources: { ref: string; kind: string }[] }
export interface Budget { id: string; available_minor: number; reserved_minor: number; used_minor: number; policy_revision: number }
export interface Coupon { id: string; percent: number; cap_minor: number; status: string }
