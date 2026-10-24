import { AppError } from './errors';

/** ₹10,00,00,000 (10 crore) in paise. Keeps every balance far below Number.MAX_SAFE_INTEGER. */
export const MAX_AMOUNT = 10_000_000_000;

/** Validates an integer amount in paise. JSON numbers only: strings, decimals, NaN etc. are rejected. */
export function parseAmount(value: unknown, field: string, min = 1): number {
  if (typeof value !== 'number' || !Number.isInteger(value)) {
    throw new AppError(400, 'INVALID_AMOUNT', `${field} must be an integer number of paise`);
  }
  if (value < 0) {
    throw new AppError(400, 'INVALID_AMOUNT', `${field} must be at least ${min}`);
  }
  if (value > MAX_AMOUNT) {
    throw new AppError(400, 'INVALID_AMOUNT', `${field} must not exceed ${MAX_AMOUNT}`);
  }
  return value;
}

export function parseString(value: unknown, field: string, maxLength = 255): string {
  if (typeof value !== 'string' || value.trim() === '' || value.length > maxLength) {
    throw new AppError(400, 'VALIDATION_ERROR', `${field} must be a non-empty string of at most ${maxLength} characters`);
  }
  return value;
}
