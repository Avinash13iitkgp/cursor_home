from __future__ import annotations

from typing import Any


def settle(expenses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return transfers that clear ``expenses``.

    Each expense is ``{"payer", "amount", "participants"}``. ``amount`` is a
    positive integer number of cents. A payer who is the only participant is
    skipped. Shares that do not divide evenly give the extra cents to the
    earliest participants.
    """
    if not isinstance(expenses, list):
        raise TypeError("expenses must be a list")
    balances: dict[str, int] = {}
    for expense in expenses:
        _apply(expense, balances)
    return _transfers(balances)


def _apply(expense: object, balances: dict[str, int]) -> None:
    if not isinstance(expense, dict):
        raise TypeError("expense must be an object")
    try:
        payer = expense["payer"]
        amount = expense["amount"]
        participants = expense["participants"]
    except KeyError as exc:
        raise ValueError("expense requires payer, amount, and participants") from exc
    if not isinstance(payer, str) or payer == "":
        raise ValueError("payer must be a non-empty string")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise ValueError("amount must be a positive integer")
    if not isinstance(participants, list) or len(participants) == 0:
        raise ValueError("participants must not be empty")
    if any(not isinstance(name, str) or name == "" for name in participants):
        raise ValueError("participants must be non-empty strings")
    if len(participants) == 1 and participants[0] == payer:
        return
    share, extra = divmod(amount, len(participants))
    for index, name in enumerate(participants):
        balances[name] = balances.get(name, 0) - share - (1 if index < extra else 0)
    balances[payer] = balances.get(payer, 0) + amount


def _transfers(balances: dict[str, int]) -> list[dict[str, Any]]:
    debtors = sorted(((name, -amount) for name, amount in balances.items() if amount < 0))
    creditors = sorted(((name, amount) for name, amount in balances.items() if amount > 0))
    transfers: list[dict[str, Any]] = []
    debtor_index = 0
    creditor_index = 0
    while debtor_index < len(debtors) and creditor_index < len(creditors):
        debtor, owed = debtors[debtor_index]
        creditor, due = creditors[creditor_index]
        payment = min(owed, due)
        transfers.append({"from": debtor, "to": creditor, "amount": payment})
        owed -= payment
        due -= payment
        if owed == 0:
            debtor_index += 1
        else:
            debtors[debtor_index] = (debtor, owed)
        if due == 0:
            creditor_index += 1
        else:
            creditors[creditor_index] = (creditor, due)
    transfers.sort(key=lambda item: (item["from"], item["to"], item["amount"]))
    return transfers
