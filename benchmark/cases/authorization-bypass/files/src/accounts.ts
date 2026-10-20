import { randomUUID } from 'node:crypto';
import { AppError } from './errors';
import type { Account, Store } from './store';

export function createAccount(store: Store, ownerId: string, openingBalance: number): Account {
  const account: Account = { id: randomUUID(), ownerId, balance: openingBalance, createdAt: new Date().toISOString() };
  store.accounts.set(account.id, account);
  return account;
}

export function listAccounts(store: Store, userId: string): Account[] {
  return [...store.accounts.values()].filter((a) => a.ownerId === userId);
}

/** Returns the account only if it exists and belongs to userId. */
export function getOwnedAccount(store: Store, userId: string, accountId: string): Account {
  const account = store.accounts.get(accountId);
  if (!account) {
    throw new AppError(404, 'ACCOUNT_NOT_FOUND', 'Account not found');
  }
  return account;
}
