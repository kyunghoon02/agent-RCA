import { CHECKOUT_RECORD } from "./curated";
import { CART_RECORD, FRONTEND_RECORD } from "./curated-states";
import {
  ADSERVICE_RECORD,
  EMAIL_RECORD,
  PAYMENT_RECORD,
  REDIS_RECORD,
  SHIPPING_RECORD,
} from "./curated-edge";
import { GENERATED_RECORDS } from "./generated";
import type { FixtureRecord } from "./helpers";

/** Sorted the way the Viewer query service sorts: updated_at DESC, incident_id DESC. */
export const FIXTURE_RECORDS: FixtureRecord[] = [
  CHECKOUT_RECORD,
  CART_RECORD,
  FRONTEND_RECORD,
  REDIS_RECORD,
  PAYMENT_RECORD,
  ADSERVICE_RECORD,
  SHIPPING_RECORD,
  EMAIL_RECORD,
  ...GENERATED_RECORDS,
].sort((left, right) => {
  if (left.incident.updated_at !== right.incident.updated_at) {
    return left.incident.updated_at < right.incident.updated_at ? 1 : -1;
  }
  return left.incident.incident_id < right.incident.incident_id ? 1 : -1;
});

export { buildDetail } from "./helpers";
export type { FixtureRecord } from "./helpers";
