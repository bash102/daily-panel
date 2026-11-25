/** Worker bindings, secrets and vars (see wrangler.toml and README.md). */
export interface Env {
  ARCHIVE: R2Bucket;
  DB: D1Database;

  // secrets (wrangler secret put ...)
  TWILIO_ACCOUNT_SID: string;
  TWILIO_AUTH_TOKEN: string;
  TWILIO_FROM_NUMBER: string;
  GRANDMA_NUMBER: string;
  PARENT_NUMBER: string;
  GRANDMA_PIN: string;
  BOX_TOKEN: string;
  MEDIA_SIGNING_KEY: string;

  // vars
  PUBLIC_BASE_URL: string;
  /** Optional: Twilio <Say> voice (default "Polly.Joanna"). */
  TWILIO_VOICE?: string;
  /** Optional: wrong PINs from all callers per hour before the line locks (default 10). */
  GLOBAL_PIN_FAILURE_LIMIT?: string;
}
