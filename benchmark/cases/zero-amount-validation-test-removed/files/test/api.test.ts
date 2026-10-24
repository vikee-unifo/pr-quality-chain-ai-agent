import request from 'supertest';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp } from '../src/app';
import type { TransferHooks } from '../src/transfers';
import { MAX_AMOUNT } from '../src/validation';

type App = ReturnType<typeof createApp>;

async function createUser(app: App, name: string) {
  const res = await request(app).post('/users').send({ name }).expect(201);
  return res.body.token as string;
}

async function createAccount(app: App, token: string, openingBalance: number) {
  const res = await request(app).post('/accounts').auth(token, { type: 'bearer' }).send({ openingBalance }).expect(201);
  return res.body.id as string;
}

async function balance(app: App, token: string, accountId: string) {
  const res = await request(app).get(`/accounts/${accountId}`).auth(token, { type: 'bearer' }).expect(200);
  return res.body.balance as number;
}

function transfer(app: App, token: string, key: string, body: object) {
  return request(app).post('/transfers').auth(token, { type: 'bearer' }).set('Idempotency-Key', key).send(body);
}

let app: App;
let hooks: TransferHooks;
let alice: string;
let bob: string;
let aliceAcc: string;
let bobAcc: string;

beforeEach(async () => {
  hooks = {};
  app = createApp({ hooks });
  alice = await createUser(app, 'Alice');
  bob = await createUser(app, 'Bob');
  aliceAcc = await createAccount(app, alice, 10_000); // ₹100.00
  bobAcc = await createAccount(app, bob, 500); // ₹5.00
});

describe('accounts', () => {
  it('creates an account owned by the caller and lists only own accounts', async () => {
    const res = await request(app).get('/accounts').auth(alice, { type: 'bearer' }).expect(200);
    expect(res.body).toHaveLength(1);
    expect(res.body[0]).toMatchObject({ id: aliceAcc, balance: 10_000 });
  });

  it('defaults opening balance to zero', async () => {
    const res = await request(app).post('/accounts').auth(alice, { type: 'bearer' }).expect(201);
    expect(res.body.balance).toBe(0);
  });

  it('rejects invalid opening balances', async () => {
    for (const openingBalance of [-1, 1.5, '100', MAX_AMOUNT + 1]) {
      await request(app).post('/accounts').auth(alice, { type: 'bearer' }).send({ openingBalance }).expect(400);
    }
  });
});

describe('authentication and authorization', () => {
  it('rejects requests without a valid token', async () => {
    await request(app).get(`/accounts/${aliceAcc}`).expect(401);
    await request(app).get(`/accounts/${aliceAcc}`).auth('bogus', { type: 'bearer' }).expect(401);
    await request(app).post('/transfers').send({ fromAccountId: aliceAcc, toAccountId: bobAcc, amount: 1 }).expect(401);
  });

  it("forbids reading another user's account", async () => {
    const res = await request(app).get(`/accounts/${aliceAcc}`).auth(bob, { type: 'bearer' }).expect(403);
    expect(res.body.error.code).toBe('FORBIDDEN');
  });

  it("forbids transferring from another user's account", async () => {
    const res = await transfer(app, bob, 'k1', { fromAccountId: aliceAcc, toAccountId: bobAcc, amount: 100 }).expect(403);
    expect(res.body.error.code).toBe('FORBIDDEN');
    expect(await balance(app, alice, aliceAcc)).toBe(10_000);
    expect(await balance(app, bob, bobAcc)).toBe(500);
  });

  it('ignores a userId supplied in the request body', async () => {
    await request(app)
      .post('/transfers')
      .auth(bob, { type: 'bearer' })
      .set('Idempotency-Key', 'k1')
      .send({ userId: 'alice', fromAccountId: aliceAcc, toAccountId: bobAcc, amount: 100 })
      .expect(403);
  });

  it('returns 404 for unknown accounts', async () => {
    await request(app).get('/accounts/does-not-exist').auth(alice, { type: 'bearer' }).expect(404);
  });
});

describe('transfers', () => {
  it('moves money between accounts', async () => {
    const res = await transfer(app, alice, 'k1', { fromAccountId: aliceAcc, toAccountId: bobAcc, amount: 2_550 }).expect(201);
    expect(res.body).toMatchObject({ fromAccountId: aliceAcc, toAccountId: bobAcc, amount: 2_550 });
    expect(res.headers['idempotent-replayed']).toBe('false');
    expect(await balance(app, alice, aliceAcc)).toBe(7_450);
    expect(await balance(app, bob, bobAcc)).toBe(3_050);
  });

  it('allows transferring the exact full balance', async () => {
    await transfer(app, alice, 'k1', { fromAccountId: aliceAcc, toAccountId: bobAcc, amount: 10_000 }).expect(201);
    expect(await balance(app, alice, aliceAcc)).toBe(0);
  });

  it('allows the minimum amount of 1 paisa', async () => {
    await transfer(app, alice, 'k1', { fromAccountId: aliceAcc, toAccountId: bobAcc, amount: 1 }).expect(201);
    expect(await balance(app, alice, aliceAcc)).toBe(9_999);
  });

  it('rejects transfers exceeding the balance by one paisa', async () => {
    const res = await transfer(app, alice, 'k1', { fromAccountId: aliceAcc, toAccountId: bobAcc, amount: 10_001 }).expect(422);
    expect(res.body.error.code).toBe('INSUFFICIENT_FUNDS');
    expect(await balance(app, alice, aliceAcc)).toBe(10_000);
    expect(await balance(app, bob, bobAcc)).toBe(500);
  });

  it('rejects transfers to the same account', async () => {
    const res = await transfer(app, alice, 'k1', { fromAccountId: aliceAcc, toAccountId: aliceAcc, amount: 1 }).expect(400);
    expect(res.body.error.code).toBe('SAME_ACCOUNT');
  });

  it('rejects transfers to a non-existent account', async () => {
    await transfer(app, alice, 'k1', { fromAccountId: aliceAcc, toAccountId: 'nope', amount: 1 }).expect(404);
    expect(await balance(app, alice, aliceAcc)).toBe(10_000);
  });

  it.each([
    ['negative', -100],
    ['decimal', 10.5],
    ['numeric string', '100'],
    ['non-numeric string', 'abc'],
    ['null', null],
    ['boolean', true],
    ['object', { value: 100 }],
    ['above maximum', MAX_AMOUNT + 1],
    ['unsafe integer', Number.MAX_SAFE_INTEGER + 2],
  ])('rejects %s amount', async (_label, amount) => {
    const res = await transfer(app, alice, 'k1', { fromAccountId: aliceAcc, toAccountId: bobAcc, amount }).expect(400);
    expect(res.body.error.code).toBe('INVALID_AMOUNT');
    expect(await balance(app, alice, aliceAcc)).toBe(10_000);
  });

  it('rejects a missing amount', async () => {
    await transfer(app, alice, 'k1', { fromAccountId: aliceAcc, toAccountId: bobAcc }).expect(400);
  });

  it('rejects malformed JSON', async () => {
    const res = await request(app)
      .post('/transfers')
      .auth(alice, { type: 'bearer' })
      .set('Idempotency-Key', 'k1')
      .set('Content-Type', 'application/json')
      .send('{"amount": ')
      .expect(400);
    expect(res.body.error.code).toBe('INVALID_JSON');
  });

  it('requires an Idempotency-Key header', async () => {
    await request(app)
      .post('/transfers')
      .auth(alice, { type: 'bearer' })
      .send({ fromAccountId: aliceAcc, toAccountId: bobAcc, amount: 100 })
      .expect(400);
  });
});

describe('idempotency', () => {
  const body = () => ({ fromAccountId: aliceAcc, toAccountId: bobAcc, amount: 1_000 });

  it('processes a retried request only once', async () => {
    const first = await transfer(app, alice, 'retry-1', body()).expect(201);
    const second = await transfer(app, alice, 'retry-1', body()).expect(201);
    expect(second.body).toEqual(first.body);
    expect(second.headers['idempotent-replayed']).toBe('true');
    expect(await balance(app, alice, aliceAcc)).toBe(9_000);
    expect(await balance(app, bob, bobAcc)).toBe(1_500);
  });

  it('handles concurrent duplicate requests once', async () => {
    const results = await Promise.all([1, 2, 3].map(() => transfer(app, alice, 'concurrent', body())));
    expect(new Set(results.map((r) => r.body.id)).size).toBe(1);
    expect(await balance(app, alice, aliceAcc)).toBe(9_000);
  });

  it('rejects reuse of a key with a different payload', async () => {
    await transfer(app, alice, 'reuse', body()).expect(201);
    const res = await transfer(app, alice, 'reuse', { ...body(), amount: 2_000 }).expect(422);
    expect(res.body.error.code).toBe('IDEMPOTENCY_KEY_REUSED');
    expect(await balance(app, alice, aliceAcc)).toBe(9_000);
  });

  it('treats different keys as different transfers', async () => {
    await transfer(app, alice, 'a', body()).expect(201);
    await transfer(app, alice, 'b', body()).expect(201);
    expect(await balance(app, alice, aliceAcc)).toBe(8_000);
  });

  it('scopes keys per user', async () => {
    await transfer(app, alice, 'shared', body()).expect(201);
    await transfer(app, bob, 'shared', { fromAccountId: bobAcc, toAccountId: aliceAcc, amount: 100 }).expect(201);
    expect(await balance(app, bob, bobAcc)).toBe(1_400);
  });

  it('does not consume the key when the transfer fails', async () => {
    await transfer(app, alice, 'k', { ...body(), amount: 20_000 }).expect(422);
    await transfer(app, alice, 'k', body()).expect(201);
  });
});

describe('atomicity', () => {
  it('rolls back the debit when a failure occurs before the credit', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    hooks.afterDebit = () => {
      throw new Error('simulated crash between debit and credit');
    };
    const res = await transfer(app, alice, 'crash', { fromAccountId: aliceAcc, toAccountId: bobAcc, amount: 1_000 }).expect(500);
    expect(res.body.error.code).toBe('INTERNAL_ERROR');
    expect(await balance(app, alice, aliceAcc)).toBe(10_000);
    expect(await balance(app, bob, bobAcc)).toBe(500);

    // The failed attempt must not be recorded: retrying the same key succeeds exactly once.
    hooks.afterDebit = undefined;
    await transfer(app, alice, 'crash', { fromAccountId: aliceAcc, toAccountId: bobAcc, amount: 1_000 }).expect(201);
    expect(await balance(app, alice, aliceAcc)).toBe(9_000);
    expect(await balance(app, bob, bobAcc)).toBe(1_500);
  });
});

it('returns a consistent 404 for unknown routes', async () => {
  const res = await request(app).get('/nope').expect(404);
  expect(res.body).toEqual({ error: { code: 'NOT_FOUND', message: 'Route not found' } });
});
