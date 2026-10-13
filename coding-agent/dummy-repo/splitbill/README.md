# splitbill

Settle shared expenses into the transfers that clear the group. Amounts are integer cents.

```bash
printf '%s\n' '[{"payer":"alice","amount":1000,"participants":["alice","bob"]}]' | python -m splitbill.cli
```

```text
bob -> alice 500
```
