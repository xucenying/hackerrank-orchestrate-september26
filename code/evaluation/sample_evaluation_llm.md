# Sample evaluation

Mode: llm | requests: 25

| Field | Exact matches |
|---|---|
| amount_safe_to_pay | 5/25 |
| affordability_status | 20/25 |
| recommended_payment_method | 22/25 |
| payment_plan | 19/25 |
| earliest_date_for_full_payment | 18/25 |
| spending_changes_needed | 22/25 |
| amount_safe_to_pay within 10% | 14/25 |

Rows with status AND method correct: 20/25
Fallback rows: 0 | repaired rows: 0

### request_01  OK
- all fields match
- explanation (ours): Pay ZAR 25,256 today. This leaves at least ZAR 18,000 available over the next 90 days.
- explanation (gt): Pay ZAR 25,256 today. This leaves at least ZAR 18,000 available over the next 90 days.

### request_02  MISS
- amount_safe_to_pay: ours='20118009.39' gt='17229139.2'
- earliest_date_for_full_payment: ours='2025-08-15' gt='2025-09-15'
- explanation (ours): Use 3 installments of IDR 15,952,906.67, starting 8 August 2025. This leaves at least IDR 29,158,400 available.
- explanation (gt): Use 3 installments of IDR 15,952,906.67, starting 8 August 2025. This leaves at least IDR 29,158,400 available.

### request_03  MISS
- amount_safe_to_pay: ours='1303285.04' gt='873000'
- payment_plan: ours='2019-10-15:5491000' gt='2019-11-15:5491000'
- earliest_date_for_full_payment: ours='2019-10-15' gt='2019-11-15'
- explanation (ours): Pay IDR 5,491,000 in full on 15 October 2019. Paying earlier would take the balance below the IDR 2,668,700 minimum.
- explanation (gt): Pay IDR 5,491,000 in full on 15 November 2019. Paying earlier would take the balance below the IDR 2,668,700 minimum.

### request_04  MISS
- amount_safe_to_pay: ours='10542318.02' gt='8401800'
- explanation (ours): Pay IDR 12,693,000 in full on 15 June 2024. Paying earlier would take the balance below the IDR 30,686,600 minimum.
- explanation (gt): Wait until 15 June 2024, then pay IDR 12,693,000 in full. Paying sooner would put the IDR 30,686,600 minimum at risk.

### request_05  MISS
- amount_safe_to_pay: ours='0' gt='737'
- explanation (ours): Do not make this payment by 12 January 2026. None of the available options keeps the ZAR 13,100 minimum protected.
- explanation (gt): Do not make this payment by 12 January 2026. None of the available options keeps the ZAR 13,100 minimum protected.

### request_06  MISS
- amount_safe_to_pay: ours='592.43' gt='603.3'
- affordability_status: ours='not_affordable' gt='affordable_with_plan'
- recommended_payment_method: ours='not_recommended' gt='full_payment'
- payment_plan: ours='none' gt='2026-01-03:620.40'
- spending_changes_needed: ours='none' gt='stop:event_476'
- explanation (ours): Do not proceed with the EUR 620.40 request. Although EUR 592.43 is available today, the full amount cannot be completed safely within 90 days while keeping the EUR 800 minimum protected.
- explanation (gt): Stop the family streaming plan, then pay EUR 620.40 today. This leaves at least EUR 800 available.

### request_07  MISS
- amount_safe_to_pay: ours='94960.66' gt='87170.56'
- explanation (ours): Use 3 installments of INR 68,432, starting 12 September 2024. This leaves at least INR 93,000 available.
- explanation (gt): Use 3 installments of INR 68,432, starting 12 September 2024. This leaves at least INR 93,000 available.

### request_08  MISS
- amount_safe_to_pay: ours='0' gt='284.57'
- affordability_status: ours='not_affordable' gt='affordable_later'
- recommended_payment_method: ours='not_recommended' gt='wait'
- payment_plan: ours='none' gt='2025-04-15:996.60'
- earliest_date_for_full_payment: ours='' gt='2025-04-15'
- explanation (ours): Do not make this payment by 15 April 2025. None of the available options keeps the EUR 800 minimum protected.
- explanation (gt): Pay EUR 996.60 in full on 15 April 2025. Paying earlier would take the balance below the EUR 800 minimum.

### request_09  OK
- all fields match
- explanation (ours): Pay EUR 166.61 today. This leaves at least EUR 600 available over the next 90 days.
- explanation (gt): Pay EUR 166.61 today. This keeps the EUR 600 minimum available over the next 90 days.

### request_10  MISS
- amount_safe_to_pay: ours='0' gt='12700'
- explanation (ours): Do not make this payment by 10 February 2025. None of the available options keeps the INR 225,400 minimum protected.
- explanation (gt): Do not make this payment by 10 February 2025. None of the available options keeps the INR 225,400 minimum protected.

### request_11  MISS
- amount_safe_to_pay: ours='13110000' gt='12510645'
- affordability_status: ours='affordable_now' gt='affordable_with_plan'
- earliest_date_for_full_payment: ours='2025-05-03' gt='2025-07-15'
- spending_changes_needed: ours='none' gt='reduce_to:event_989:665950'
- explanation (ours): Pay IDR 13,110,000 today. This leaves at least IDR 34,140,600 available over the next 90 days.
- explanation (gt): Reduce the weekend food delivery to IDR 665,950, then pay IDR 13,110,000 today. This leaves at least IDR 34,140,600 available.

### request_12  OK
- all fields match
- explanation (ours): Use 3 installments of ZAR 22,590.19, starting 19 April 2026. This leaves at least ZAR 43,200 available.
- explanation (gt): Use 3 installments of ZAR 22,590.19, starting 19 April 2026. This leaves at least ZAR 43,200 available.

### request_13  MISS
- amount_safe_to_pay: ours='941.6' gt='433.4'
- affordability_status: ours='affordable_now' gt='affordable_later'
- recommended_payment_method: ours='full_payment' gt='wait'
- payment_plan: ours='2024-03-07:941.60' gt='2024-05-15:941.60'
- earliest_date_for_full_payment: ours='2024-03-07' gt='2024-05-15'
- explanation (ours): Pay EUR 941.60 today. This leaves at least EUR 1,300 available over the next 90 days.
- explanation (gt): Pay EUR 941.60 in full on 15 May 2024. Paying earlier would take the balance below the EUR 1,300 minimum.

### request_14  MISS
- amount_safe_to_pay: ours='628.21' gt='597.74'
- explanation (ours): Do not proceed with the EUR 5,414.20 request. Although EUR 628.21 is available today, the full amount cannot be completed while protecting the EUR 2,200 minimum balance.
- explanation (gt): Do not proceed with the EUR 5,414.20 request. Although EUR 597.74 is available today, the full amount cannot be completed safely within 90 days.

### request_15  MISS
- amount_safe_to_pay: ours='6.54' gt='83.05'
- explanation (ours): Do not proceed with the EUR 3,685 request. Although EUR 6.54 is available today, the full amount cannot be completed while protecting the EUR 1,200 minimum balance.
- explanation (gt): Do not make this payment by 1 February 2026. None of the available options keeps the EUR 1,200 minimum protected.

### request_16  OK
- all fields match
- explanation (ours): Pay INR 122,500 today. This leaves at least INR 122,400 available over the next 90 days.
- explanation (gt): Pay INR 122,500 today. This leaves at least INR 122,400 available over the next 90 days.

### request_17  OK
- all fields match
- explanation (ours): Use 3 installments of INR 95,194.67, starting 1 March 2026. This leaves at least INR 166,100 available.
- explanation (gt): Use 3 installments of INR 95,194.67, starting 1 March 2026. This leaves at least INR 166,100 available.

### request_18  MISS
- amount_safe_to_pay: ours='646.35' gt='462'
- payment_plan: ours='2026-08-15:3246.10' gt='2026-09-15:3246.10'
- earliest_date_for_full_payment: ours='2026-08-15' gt='2026-09-15'
- explanation (ours): Pay EUR 3,246.10 in full on 15 August 2026. Paying earlier would take the balance below the EUR 1,400 minimum.
- explanation (gt): Pay EUR 3,246.10 in full on 15 September 2026. Paying earlier would take the balance below the EUR 1,400 minimum.

### request_19  MISS
- amount_safe_to_pay: ours='30156.18' gt='28820'
- payment_plan: ours='2024-09-04:30156.18|2024-09-15:9503.82' gt='2024-09-04:28820|2024-09-15:10840'
- explanation (ours): Pay INR 30,156.18 today and the remaining INR 9,503.82 on 15 September 2024. This completes the full request and keeps the INR 92,800 minimum protected.
- explanation (gt): Pay INR 28,820 today and the remaining INR 10,840 on 15 September 2024. This completes the full request and keeps the INR 92,800 minimum protected.

### request_20  MISS
- amount_safe_to_pay: ours='12576.59' gt='5400'
- explanation (ours): Do not proceed with the INR 303,700 request. The full amount cannot be completed safely within 90 days while keeping the INR 64,500 minimum protected.
- explanation (gt): Do not make this payment by 22 February 2026. None of the available options keeps the INR 64,500 minimum protected.

### request_21  MISS
- amount_safe_to_pay: ours='1574.4' gt='1543.35'
- affordability_status: ours='affordable_now' gt='affordable_with_plan'
- earliest_date_for_full_payment: ours='2026-04-03' gt='2026-04-15'
- spending_changes_needed: ours='none' gt='stop:event_1815|reduce_to:event_1816:23.50'
- explanation (ours): Pay USD 1,574.40 today. This leaves at least USD 1,800 available over the next 90 days.
- explanation (gt): Stop the online backup subscription and reduce the streaming subscription to USD 23.50, then pay USD 1,574.40 today. This leaves at least USD 1,800 available.

### request_22  MISS
- amount_safe_to_pay: ours='465.27' gt='475.46'
- explanation (ours): Use 3 installments of EUR 253.59, starting 8 December 2024. This leaves at least EUR 500 available.
- explanation (gt): Use 3 installments of EUR 253.59, starting 8 December 2024. This leaves at least EUR 500 available.

### request_23  MISS
- amount_safe_to_pay: ours='8406.78' gt='9152'
- explanation (ours): Pay ZAR 38,016 in full on 15 July 2025. Paying earlier would take the balance below the ZAR 27,000 minimum.
- explanation (gt): Pay ZAR 38,016 in full on 15 July 2025. Paying earlier would take the balance below the ZAR 27,000 minimum.

### request_24  MISS
- amount_safe_to_pay: ours='17170.58' gt='13420'
- explanation (ours): Do not proceed with the INR 109,600 request. The full amount cannot be completed safely within 90 days while keeping the INR 51,000 minimum protected.
- explanation (gt): Do not proceed with the INR 109,600 request. Although INR 13,420 is available today, the full amount cannot be completed safely within 90 days.

### request_25  MISS
- amount_safe_to_pay: ours='1546215.19' gt='1425000'
- explanation (ours): Do not proceed with the IDR 60,496,000 request. Only IDR 1,546,215.19 is available while keeping the IDR 23,379,100 minimum balance protected.
- explanation (gt): Do not make this payment by 17 April 2024. None of the available options keeps the IDR 23,379,100 minimum protected.
