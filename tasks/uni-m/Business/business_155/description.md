**Task Requirements:**

Execute an employee grievance handling and resolution workflow spanning grievance submission and investigation in Frappe HRMS, legal/advisory cost accounting in BigCapital, event-based training session for policy awareness in Pretix, and confidential investigation task management in Twenty CRM.

In Frappe HRMS: (1) Navigate to the Grievance Type list. Create a grievance type 'Workplace Harassment' if it does not already exist. Create a second grievance type 'Retaliation' if it does not already exist. (2) Create an Employee Grievance for employee 'Pooja Malhotra' (HR-EMP-00008) with grievance type 'Workplace Harassment', grievance against party type 'Employee', grievance against 'Arjun Nair' (HR-EMP-00011), subject 'Repeated hostile behavior in team meetings', and description 'The grievant has reported a pattern of hostile and intimidating behavior by the respondent during weekly project meetings, creating an unsafe work environment.'. (3) Navigate to the Employee Grievance list and verify the grievance for 'Pooja Malhotra' exists with status 'Open'. (4) Navigate to the employee record for 'Pooja Malhotra' (HR-EMP-00008) and verify their department is 'Human Resources - TVS' and designation is 'HR Executive'. (5) Navigate to the employee record for 'Arjun Nair' (HR-EMP-00011) and verify their department is 'Sales & Marketing - TVS'. (6) Create an Employee Transfer record for 'Arjun Nair' (HR-EMP-00011) with transfer date 2025-07-01, changing department from 'Sales & Marketing - TVS' to 'Customer Service - TVS'. Submit the transfer. (7) Navigate to the Employee Information report filtered by department 'Customer Service - TVS'. Verify 'Arjun Nair' appears in this department. (8) Create a Training Program named 'Workplace Policy Compliance 2025' with description 'Workplace policy compliance training - triggered by grievance investigation'. (9) Create a Training Event named 'Policy Awareness Workshop - Q3 2025' linked to training program 'Workplace Policy Compliance 2025', event start date 2025-07-15, end date 2025-07-15, type 'Workshop'. Add exactly 3 employees as participants: Pooja Malhotra, Arjun Nair, and Rajesh Kumar.

In BigCapital: (10) Create an account 'Legal and Advisory Fees' of type 'Expense' if it does not already exist. (11) Create an expense entry dated 2025-07-05 for amount 2800 under expense account 'Legal and Advisory Fees' paid from 'Bank Account', with reference 'External investigation advisory - grievance Repeated hostile behavior in team meetings'. Publish the expense. (12) Create a second expense entry dated 2025-07-20 for amount 1700 under expense account 'Legal and Advisory Fees' paid from 'Bank Account', with reference 'Mediation services - grievance resolution'. Publish the expense. (13) Navigate to the General Ledger report filtered to account 'Legal and Advisory Fees' for date range 2025-07-05 to 2025-07-20. Verify two entries appear: 2800 and 1700, totaling 4500. (14) Navigate to the Profit and Loss Sheet for date range 2025-07-05 to 2025-07-20. Verify 'Legal and Advisory Fees' shows 4500.

In Pretix: (15) Create a new event 'Workplace Policy Compliance Workshop' with slug 'policy-compliance-workshop' under organizer 'edu-workshop', start date 2025-07-15, currency 'USD'. (16) Create a product 'Policy Training Admission' priced at 0 (free internal training). (17) Create a quota 'Training Capacity' with size 50 linked to 'Policy Training Admission'. (18) Create a custom question of type 'Text (one line)' with text 'Employee ID' required for 'Policy Training Admission'. (19) Create a custom question of type 'Choice (single)' with text 'Department' with options 'Human Resources', 'Sales & Marketing', 'Customer Service', required for 'Policy Training Admission'. (20) Create a check-in list named 'Training Attendance Check-in' linked to 'Policy Training Admission'. (21) Set the event to live.

In Twenty CRM: (22) Create a task titled 'CONFIDENTIAL: Investigate grievance - Repeated hostile behavior in team meetings' with due date 2025-07-12 and body: 'Grievant: Pooja Malhotra (HR-EMP-00008), dept Human Resources - TVS. Respondent: Arjun Nair (HR-EMP-00011), dept Sales & Marketing - TVS. Type: Workplace Harassment. Description: The grievant has reported a pattern of hostile and intimidating behavior by the respondent during weekly project meetings, creating an unsafe work environment.. Investigation advisory cost: 2800 USD. Deadline for findings: 2025-07-12.' (23) Create a task titled 'CONFIDENTIAL: Mediation session - Pooja Malhotra and Arjun Nair' with due date 2025-07-25 and body: 'Schedule mediation between Pooja Malhotra and Arjun Nair. Mediation cost: 1700 USD. Arjun Nair transferred to Customer Service - TVS effective 2025-07-01 as interim measure.' (24) Create a task titled 'Mandatory compliance training - Workplace Policy Compliance Workshop' with due date 2025-07-15 and body: 'All-hands policy training on 2025-07-15. 3 employees enrolled in HRMS. Pretix registration live for attendance tracking. Check-in list: Training Attendance Check-in. Ensure 100% attendance.' (25) Create a note titled 'Grievance Resolution Log - Repeated hostile behavior in team meetings - 2025-07-12' with body:
'CASE DETAILS:
Grievant: Pooja Malhotra (HR-EMP-00008) - Human Resources - TVS, HR Executive
Respondent: Arjun Nair (HR-EMP-00011) - originally Sales & Marketing - TVS
Type: Workplace Harassment
Subject: Repeated hostile behavior in team meetings

ACTIONS TAKEN:
1. Grievance filed and recorded in HRMS (status: Open)
2. Arjun Nair transferred to Customer Service - TVS effective 2025-07-01
3. External investigation advisory engaged: 2800 USD on 2025-07-05
4. Mediation services engaged: 1700 USD on 2025-07-20
5. Total legal/advisory cost: 4500 USD (account: Legal and Advisory Fees)
6. Mandatory compliance training scheduled: Workplace Policy Compliance Workshop on 2025-07-15
7. 3 employees enrolled, Pretix check-in configured'

**Steps:**

1. In Frappe HRMS, create grievance types 'Workplace Harassment' and 'Retaliation' if they do not exist, then submit an employee grievance for 'Pooja Malhotra' against 'Arjun Nair' and verify the grievance is Open and both employees' department/designation details match.
2. In Frappe HRMS, create and submit an Employee Transfer moving 'Arjun Nair' from 'Sales & Marketing - TVS' to 'Customer Service - TVS', verify via the Employee Information report, then create a Training Program and Training Event with exactly 3 named attendees.
3. In BigCapital, create the expense account if needed, record and publish two expense entries (investigation advisory and mediation services) under 'Legal and Advisory Fees', then verify the General Ledger shows both entries and the Profit and Loss Sheet shows the total.
4. In Pretix, create a free policy training event with a product, quota, two custom questions (Employee ID text field and Department single-choice), a check-in list, and set the event to live.
5. In Twenty CRM, create three tasks (confidential investigation, confidential mediation, mandatory training) and a detailed grievance resolution log note summarizing all actions taken across all four applications.

**Login Credentials:**

- frappe-hrms: Administrator / admin
- bigcapital: admin@bigcapital.local / admin123
- pretix: admin@localhost / admin
- twenty: jony.ive@apple.dev / tim@apple.dev
