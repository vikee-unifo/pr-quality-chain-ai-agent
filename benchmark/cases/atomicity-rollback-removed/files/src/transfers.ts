import { randomUUID } from 'node:crypto';
import { getOwnedAccount } from './accounts';
import { AppError } from './errors';
import type { Store, Transfer } from './store';

export type TransferInput = { fromAccountId: string; toAccountId: string; amount: number };

/** Test seam: lets tests inject a failure between the debit and the credit. */
export type TransferHooks = { afterDebit?: () => void };

export type TransferResult = { transfer: Transfer; replayed: boolean };

/**
 * Moves `amount` paise from one of the caller's accounts to any existing account.
 *
 * Idempotency: keys are scoped per user. A repeated key with the same payload returns the
 * original transfer without moving money again. A repeated key with a different payload is
 * rejected with 422. Only successful transfers consume a key, so a failed request may be retried.
 *
 * ponytail: atomicity relies on this function being synchronous in single-threaded Node,
 * so no other request can interleave. With a real database, use one DB transaction with row locks.
 */
export function executeTransfer(
  store: Store,
  userId: string,
  idempotencyKey: string,
  input: TransferInput,
  hooks: TransferHooks = {},
): TransferResult {
  const scopedKey = `${userId}:${idempotencyKey}`;
  const requestHash = JSON.stringify([input.fromAccountId, input.toAccountId, input.amount]);

  const previous = store.idempotency.get(scopedKey);
  if (previous) {
    if (previous.requestHash !== requestHash) {
      throw new AppError(422, 'IDEMPOTENCY_KEY_REUSED', 'Idempotency key was already used with a different request');
    }
    return { transfer: store.transfers.get(previous.transferId)!, replayed: true };
  }

  if (input.fromAccountId === input.toAccountId) {
    throw new AppError(400, 'SAME_ACCOUNT', 'Cannot transfer to the same account');
  }
  const from = getOwnedAccount(store, userId, input.fromAccountId);
  const to = store.accounts.get(input.toAccountId);
  if (!to) {
    throw new AppError(404, 'ACCOUNT_NOT_FOUND', 'Destination account not found');
  }
  if (from.balance < input.amount) {
    throw new AppError(422, 'INSUFFICIENT_FUNDS', 'Insufficient balance');
  }
  if (!Number.isSafeInteger(to.balance + input.amount)) {
    throw new AppError(422, 'BALANCE_LIMIT', 'Destination balance would exceed the supported limit');
  }

  from.balance -= input.amount;
  hooks.afterDebit?.();
  to.balance += input.amount;

  const transfer: Transfer = { id: randomUUID(), ...input, createdAt: new Date().toISOString() };
  store.transfers.set(transfer.id, transfer);
  store.idempotency.set(scopedKey, { requestHash, transferId: transfer.id });
  return { transfer, replayed: false };
}
