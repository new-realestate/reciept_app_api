# Roles, permissions and data access

Every rule here is enforced on the server in `app/core/access.py` and in the routers. Hiding
buttons in the portal is only a convenience.

## Ownership

```
Business (tenants table) ─┬─ Team members (users)
                          ├─ Properties ── Units
                          │      └── Staff assignments (who may manage the property)
                          ├─ Customers ── Renter profile (optional, one per customer)
                          │                   └── Tenancies (property + optional unit, history kept)
                          └─ Receipts / Invoices / Quotations / Payment schedules
                                 └── optional property_id / unit_id / tenancy_id
```

- In the code, `tenant_id` always means **the business**. The person or company renting a
  property is a **Renter**; the portal calls them "Tenants".
- Records without a property are **general business transactions**. A customer never needs to
  be a renter to receive a receipt, invoice or quotation.
- Composite foreign keys `(tenant_id, x_id)` make the database reject links between two
  businesses.

## Roles

| Capability | Owner | Admin | Staff |
|---|---|---|---|
| Business profile, logo, bank details, reminder defaults | ✓ | ✓ | – |
| Create and manage staff | ✓ | staff only | – |
| Create or manage admins | ✓ | – | – |
| Create, edit and archive properties and units; assign staff | ✓ | ✓ | – |
| See properties | all | all | assigned only |
| Cancel invoices and quotations; rotate or revoke public links | ✓ | ✓ | – |
| Audit log | whole business | whole business | – (the dashboard shows their own activity) |
| Everything else | ✓ | ✓ | by permission flag (below) |

Nobody can change their own role, permissions or active state. The owner can't be edited,
demoted or deactivated through the team API.

## Staff permission flags

`users.permissions` holds a list of flags. `NULL` means the default set.

| Flag | Default for staff | Allows |
|---|---|---|
| `customers.view` | ✓ | Customer directory and document history |
| `customers.manage` | ✓ | Create, edit and archive customers |
| `renters.manage` | ✓ | Tenants and tenancies on assigned properties; edit, upload and submit their KYC |
| `renters.kyc_review` | – | Review tenant KYC on assigned properties, and open the documents |
| `receipts.issue` | ✓ | Issue and send receipts |
| `invoices.issue` | ✓ | Create and issue invoices; payment schedules; convert quotations |
| `invoices.record_payment` | ✓ | Record payments against invoices |
| `quotations.issue` | ✓ | Create and send quotations |
| `documents.view` | ✓ | See general documents (those with no property) |
| `reports.view` | – | Reserved for reporting |

What staff can see:
- Documents **linked to a property** are visible only while the staff member is assigned to that
  property.
- **General** documents need `documents.view`.
- Revoking an assignment, changing permissions or deactivating a member takes effect on their
  next request.
- Anything out of scope returns **404**, so ids can't be probed.

## KYC

Statuses: `not_submitted → pending_review → verified | rejected | requires_correction`.
`expired` is derived when a verified profile's ID expiry date has passed. Editing identity
details on a verified or pending profile moves it back to `not_submitted`.

| | Edit / upload / submit | Review | Open documents |
|---|---|---|---|
| Staff member's own profile | themselves, owner, admin | owner/admin (for an admin's profile: owner; for an owner's: another owner) | themselves and reviewers |
| Tenant profile | owner, admin, staff with `renters.manage` on the tenant's property | owner, admin, staff with `renters.kyc_review` on the property (never their own submission) | reviewers only |

- Verification is a **manual** review. It is separate from account activation and from tenancy
  status.
- ID numbers are encrypted with Fernet (`KYC_ENCRYPTION_KEY`). API responses only include
  `••••1234`. `POST /kyc/profiles/{id}/reveal-id-number` is available to reviewers and is
  audited.
- Files are stored under `private/kyc/…`:
  - The public `/static` route refuses that prefix.
  - Downloads go through `POST /kyc/documents/{id}/download-url`, which returns a link valid for
    5 minutes. Access is checked again every time the link is used.
  - The file is served with `Cache-Control: no-store`, and every access is audited
    (`kyc.document_accessed`).
- Uploads are checked by their file signature: PDF, PNG, JPEG or WebP, at most 10 MB.
- **Retention:** owners/admins can delete a document. The file is removed and the metadata row
  is kept. There is no automatic purge yet; agree a retention period that fits local law (for
  example, delete documents some time after a tenancy ends or a member leaves) and apply it
  manually.

## Receipts

- Creating a receipt requires `payment_received_confirmed: true`. The app records payments
  received elsewhere; it never moves money.
- `paid_at` can't be in the future.
- An `Idempotency-Key` header (or `idempotency_key` field) returns the original receipt instead
  of creating a duplicate.
- Numbers come from the per-business `document_counters` row (`kind = 'receipt'`), locked for
  each allocation:
  - The format is unchanged (`RCT-YYYY-NNNNNN`) and the sequence never reuses a number.
  - Migration `0006` seeds the counter from the highest existing number.
- A receipt for an invoice payment inherits the invoice's property, unit and tenancy.
