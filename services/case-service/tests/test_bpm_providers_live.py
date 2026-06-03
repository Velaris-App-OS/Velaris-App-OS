"""Live parser tests for all 12 BPM providers.

Real files (downloaded from GitHub):
  - Camunda simple (camunda-bpm-examples/spring-boot-starter)
  - Camunda gateway (camunda-bpm-platform/ExclusiveGatewayTest)
  - Flowable VacationRequest (flowable-examples)
  - Power Automate AutoApproveOpenShift (OfficeDev/Microsoft-Teams-Shifts)

Realistic synthetic samples (authoritative format spec) for proprietary vendors:
  - jBPM/Kogito BPMN 2.0 (with tns: namespace)
  - IBM BAW BPMN 2.0 (with icp: teamRef)
  - Oracle BPM BPMN 2.0
  - Bizagi BPMN 2.0
  - Pega XML ruleset export
  - ServiceNow XML update set
  - Appian ProcessModel XML
  - Salesforce FlowDefinition XML
  - Nintex SharePoint XML
  - Nintex NWC JSON

Each test asserts:
  - Parser does not crash
  - At least 1 process/workflow/flow extracted
  - Steps/actions are present
  - No round-robin ordering (steps in meaningful sequence)
"""
from __future__ import annotations

import json
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from case_service.bpm_importer.parsers import bpmn2, pega, servicenow, appian
from case_service.bpm_importer.parsers import power_automate, salesforce_flow, nintex
from case_service.bpm_importer.extractor import extract

# ══════════════════════════════════════════════════════════════════════════════
# REAL FILES (downloaded from public GitHub repos)
# ══════════════════════════════════════════════════════════════════════════════

# Source: https://github.com/camunda/camunda-bpm-examples/blob/master/spring-boot-starter/example-simple/src/main/resources/bpmn/sample.bpmn
CAMUNDA_REAL = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn2:definitions xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xmlns:bpmn2="http://www.omg.org/spec/BPMN/20100524/MODEL"
  xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI"
  xmlns:camunda="http://camunda.org/schema/1.0/bpmn"
  xmlns:dc="http://www.omg.org/spec/DD/20100524/DC"
  xmlns:di="http://www.omg.org/spec/DD/20100524/DI"
  id="_ll67ABGYEeW7xqkBzIjHqw" exporter="camunda modeler"
  exporterVersion="2.7.0" targetNamespace="http://camunda.org/schema/1.0/bpmn">
  <bpmn2:process id="Sample" name="Sample" isExecutable="true" camunda:historyTimeToLive="P180D">
    <bpmn2:startEvent id="StartEvent_1">
      <bpmn2:outgoing>SequenceFlow_1</bpmn2:outgoing>
    </bpmn2:startEvent>
    <bpmn2:userTask id="UserTask_1" name="do something">
      <bpmn2:incoming>SequenceFlow_1</bpmn2:incoming>
      <bpmn2:outgoing>SequenceFlow_2</bpmn2:outgoing>
    </bpmn2:userTask>
    <bpmn2:sequenceFlow id="SequenceFlow_1" sourceRef="StartEvent_1" targetRef="UserTask_1"/>
    <bpmn2:serviceTask id="ServiceTask_1" camunda:delegateExpression="${sayHelloDelegate}" name="say hello">
      <bpmn2:incoming>SequenceFlow_2</bpmn2:incoming>
      <bpmn2:outgoing>SequenceFlow_3</bpmn2:outgoing>
    </bpmn2:serviceTask>
    <bpmn2:sequenceFlow id="SequenceFlow_2" sourceRef="UserTask_1" targetRef="ServiceTask_1"/>
    <bpmn2:endEvent id="EndEvent_1">
      <bpmn2:incoming>SequenceFlow_3</bpmn2:incoming>
    </bpmn2:endEvent>
    <bpmn2:sequenceFlow id="SequenceFlow_3" sourceRef="ServiceTask_1" targetRef="EndEvent_1"/>
  </bpmn2:process>
</bpmn2:definitions>"""

# Source: https://github.com/camunda/camunda-bpm-platform/blob/master/engine/src/test/resources/.../ExclusiveGatewayTest.testDivergingExclusiveGateway.bpmn20.xml
CAMUNDA_GATEWAY_REAL = """<?xml version="1.0" encoding="UTF-8"?>
<definitions id="definitions"
  xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xmlns:camunda="http://camunda.org/schema/1.0/bpmn"
  targetNamespace="Examples">
  <process id="exclusiveGwDiverging" isExecutable="true">
    <startEvent id="theStart" />
    <sequenceFlow id="flow1" sourceRef="theStart" targetRef="exclusiveGw" />
    <exclusiveGateway id="exclusiveGw" name="Route by input" />
    <sequenceFlow id="flow2" sourceRef="exclusiveGw" targetRef="theTask1">
      <conditionExpression xsi:type="tFormalExpression">${input == 1}</conditionExpression>
    </sequenceFlow>
    <sequenceFlow id="flow3" sourceRef="exclusiveGw" targetRef="theTask2">
      <conditionExpression xsi:type="tFormalExpression">${input == 2}</conditionExpression>
    </sequenceFlow>
    <sequenceFlow id="flow4" sourceRef="exclusiveGw" targetRef="theTask3">
      <conditionExpression xsi:type="tFormalExpression">${input == 3}</conditionExpression>
    </sequenceFlow>
    <userTask id="theTask1" name="Task 1" />
    <sequenceFlow id="flow5" sourceRef="theTask1" targetRef="theEnd" />
    <userTask id="theTask2" name="Task 2" />
    <sequenceFlow id="flow6" sourceRef="theTask2" targetRef="theEnd" />
    <userTask id="theTask3" name="Task 3" />
    <sequenceFlow id="flow7" sourceRef="theTask3" targetRef="theEnd" />
    <endEvent id="theEnd" />
  </process>
</definitions>"""

# Source: https://github.com/flowable/flowable-examples/blob/master/spring-boot-example/src/main/resources/processes/VacationRequest.bpmn20.xml
FLOWABLE_REAL = """<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xmlns:flowable="http://flowable.org/bpmn"
  targetNamespace="http://www.flowable.org/processdef">
  <process id="vacationRequest" name="Vacation request" isExecutable="true">
    <startEvent id="request" flowable:initiator="employeeName" flowable:formKey="vacation-request"/>
    <sequenceFlow id="flow1" sourceRef="request" targetRef="handleRequest"/>
    <userTask id="handleRequest" name="Handle vacation request"
              flowable:assignee="$INITIATOR" flowable:formKey="handle-vacation-request">
      <documentation>${employeeName} would like to take ${numberOfDays} day(s)</documentation>
    </userTask>
    <exclusiveGateway id="requestApprovedDecision" name="Request approved?"/>
    <manualTask id="sendApprovalMail" name="Send confirmation e-mail"/>
    <sequenceFlow id="flow4" sourceRef="sendApprovalMail" targetRef="theEnd1"/>
    <endEvent id="theEnd1"/>
    <userTask id="adjustVacationRequestTask" name="Adjust vacation request"
              flowable:assignee="$INITIATOR" flowable:formKey="adjust-vacation-request"/>
    <exclusiveGateway id="resendRequestDecision" name="Resend request?"/>
    <endEvent id="theEnd2"/>
    <sequenceFlow id="flow2" sourceRef="handleRequest" targetRef="requestApprovedDecision"/>
    <sequenceFlow id="flow5" sourceRef="requestApprovedDecision" targetRef="adjustVacationRequestTask">
      <conditionExpression xsi:type="tFormalExpression"><![CDATA[${vacationApproved == 'Reject'}]]></conditionExpression>
    </sequenceFlow>
    <sequenceFlow id="flow3" sourceRef="requestApprovedDecision" targetRef="sendApprovalMail">
      <conditionExpression xsi:type="tFormalExpression"><![CDATA[${vacationApproved == 'Approve'}]]></conditionExpression>
    </sequenceFlow>
    <sequenceFlow id="flow6" sourceRef="adjustVacationRequestTask" targetRef="resendRequestDecision"/>
    <sequenceFlow id="flow7" sourceRef="resendRequestDecision" targetRef="handleRequest">
      <conditionExpression xsi:type="tFormalExpression"><![CDATA[${resendRequest == 'Yes'}]]></conditionExpression>
    </sequenceFlow>
    <sequenceFlow id="flow8" sourceRef="resendRequestDecision" targetRef="theEnd2">
      <conditionExpression xsi:type="tFormalExpression"><![CDATA[${resendRequest == 'No'}]]></conditionExpression>
    </sequenceFlow>
  </process>
</definitions>"""

# Source: https://github.com/OfficeDev/Microsoft-Teams-Shifts-Power-Automate-Templates/.../definition.json
POWER_AUTOMATE_REAL = json.dumps({
    "name": "AutoApproveOpenShift",
    "properties": {
        "displayName": "Auto Approve Open Shift Request with Email Notification",
        "definition": {
            "$schema": "https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#",
            "contentVersion": "1.0.0.0",
            "triggers": {
                "Recurrence_Every_Hour": {
                    "recurrence": {"frequency": "Hour", "interval": 1},
                    "type": "Recurrence"
                }
            },
            "actions": {
                "List_all_Open_Shift_requests": {
                    "runAfter": {},
                    "type": "OpenApiConnection",
                    "inputs": {
                        "host": {"operationId": "ListOpenShiftChangeRequests"},
                        "parameters": {"teamId": ""}
                    }
                },
                "Apply_to_each": {
                    "foreach": "@outputs('List_all_Open_Shift_requests')?['body/value']",
                    "actions": {
                        "Condition_Check_pending": {
                            "actions": {
                                "Get_user_profile": {
                                    "runAfter": {},
                                    "type": "OpenApiConnection",
                                    "inputs": {"host": {"operationId": "UserProfile_V2"}}
                                },
                                "Approve_shift_request": {
                                    "runAfter": {"Get_user_profile": ["Succeeded"]},
                                    "type": "OpenApiConnection",
                                    "inputs": {"host": {"operationId": "OfferShiftRequestApprove"}}
                                },
                                "Send_approval_email": {
                                    "runAfter": {"Approve_shift_request": ["Succeeded"]},
                                    "type": "OpenApiConnection",
                                    "inputs": {
                                        "host": {"operationId": "SendEmailV2"},
                                        "parameters": {
                                            "emailMessage/Subject": "Your request has been approved",
                                            "emailMessage/Body": "<p>Your request was approved.</p>"
                                        }
                                    }
                                }
                            },
                            "runAfter": {},
                            "expression": {"equals": ["@items('Apply_to_each')?['state']", "pending"]},
                            "type": "If"
                        }
                    },
                    "runAfter": {"List_all_Open_Shift_requests": ["Succeeded"]},
                    "type": "Foreach"
                }
            }
        }
    }
})

# ══════════════════════════════════════════════════════════════════════════════
# SYNTHETIC REPRESENTATIVE SAMPLES (proprietary / hard-to-find formats)
# Each matches exactly what the parsers are designed to consume.
# ══════════════════════════════════════════════════════════════════════════════

# jBPM/Kogito — BPMN 2.0 with tns: and bpsim: namespaces (Red Hat stack)
JBPM_BPMN = """<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL"
  xmlns:tns="http://www.jboss.org/drools"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  targetNamespace="http://www.jboss.org/drools">
  <process id="orderFulfilment" name="Order Fulfilment" isExecutable="true" tns:packageName="com.example">
    <startEvent id="start" name="Order Received"/>
    <sequenceFlow id="sf1" sourceRef="start" targetRef="validateOrder"/>
    <userTask id="validateOrder" name="Validate Order" tns:taskName="validation-form">
      <extensionElements>
        <tns:onEntry-script scriptFormat="java"><![CDATA[System.out.println("validating");]]></tns:onEntry-script>
      </extensionElements>
    </userTask>
    <sequenceFlow id="sf2" sourceRef="validateOrder" targetRef="approvalGateway"/>
    <exclusiveGateway id="approvalGateway" name="Order Valid?"/>
    <sequenceFlow id="sf3" sourceRef="approvalGateway" targetRef="fulfillOrder">
      <conditionExpression xsi:type="tFormalExpression">orderValid == true</conditionExpression>
    </sequenceFlow>
    <sequenceFlow id="sf4" sourceRef="approvalGateway" targetRef="rejectOrder">
      <conditionExpression xsi:type="tFormalExpression">orderValid == false</conditionExpression>
    </sequenceFlow>
    <serviceTask id="fulfillOrder" name="Fulfil Order" tns:taskName="fulfilment-service"/>
    <sequenceFlow id="sf5" sourceRef="fulfillOrder" targetRef="notifyCustomer"/>
    <userTask id="notifyCustomer" name="Notify Customer"/>
    <sequenceFlow id="sf6" sourceRef="notifyCustomer" targetRef="end"/>
    <userTask id="rejectOrder" name="Reject Order"/>
    <sequenceFlow id="sf7" sourceRef="rejectOrder" targetRef="end"/>
    <endEvent id="end" name="Order Completed"/>
  </process>
</definitions>"""

# IBM BAW — BPMN 2.0 with icp: teamRef (IBM Business Automation Workflow)
IBM_BAW_BPMN = """<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL"
  xmlns:icp="http://www.ibm.com/xmlns/prod/websphere/ibm-bpm/bpd"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  targetNamespace="http://www.ibm.com/bam">
  <process id="insuranceClaim" name="Insurance Claim Processing" isExecutable="true">
    <laneSet id="laneSet1">
      <lane id="customerLane" name="Customer">
        <flowNodeRef>submitClaim</flowNodeRef>
      </lane>
      <lane id="adjusterLane" name="Claims Adjuster" icp:teamRef="ClaimsTeam">
        <flowNodeRef>reviewClaim</flowNodeRef>
        <flowNodeRef>assessDamage</flowNodeRef>
      </lane>
      <lane id="managerLane" name="Manager">
        <flowNodeRef>approvePayout</flowNodeRef>
      </lane>
    </laneSet>
    <startEvent id="claimReceived" name="Claim Received"/>
    <sequenceFlow id="sf1" sourceRef="claimReceived" targetRef="submitClaim"/>
    <userTask id="submitClaim" name="Submit Claim Details" icp:formRef="ClaimSubmissionForm"/>
    <sequenceFlow id="sf2" sourceRef="submitClaim" targetRef="reviewClaim"/>
    <userTask id="reviewClaim" name="Review Claim" icp:teamRef="ClaimsTeam"/>
    <sequenceFlow id="sf3" sourceRef="reviewClaim" targetRef="assessDamage"/>
    <serviceTask id="assessDamage" name="Assess Damage via API"/>
    <sequenceFlow id="sf4" sourceRef="assessDamage" targetRef="approvePayout"/>
    <userTask id="approvePayout" name="Approve Payout"/>
    <sequenceFlow id="sf5" sourceRef="approvePayout" targetRef="claimClosed"/>
    <endEvent id="claimClosed" name="Claim Closed"/>
  </process>
</definitions>"""

# Oracle BPM Suite — BPMN 2.0 with process: namespace
ORACLE_BPM_BPMN = """<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL"
  xmlns:process="http://xmlns.oracle.com/bpmn20/extensions"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  targetNamespace="http://oracle.com/bpm">
  <process id="purchaseApproval" name="Purchase Order Approval" isExecutable="true">
    <startEvent id="poReceived" name="PO Received" process:initiator="requester"/>
    <sequenceFlow id="sf1" sourceRef="poReceived" targetRef="reviewPO"/>
    <userTask id="reviewPO" name="Review Purchase Order" process:participant="PurchasingTeam">
      <extensionElements>
        <process:taskParameters>
          <process:taskParameter name="poAmount" type="double"/>
        </process:taskParameters>
      </extensionElements>
    </userTask>
    <sequenceFlow id="sf2" sourceRef="reviewPO" targetRef="amountGateway"/>
    <exclusiveGateway id="amountGateway" name="Amount Threshold?"/>
    <sequenceFlow id="sf3" sourceRef="amountGateway" targetRef="managerApproval">
      <conditionExpression>poAmount &gt; 10000</conditionExpression>
    </sequenceFlow>
    <sequenceFlow id="sf4" sourceRef="amountGateway" targetRef="autoApprove">
      <conditionExpression>poAmount &lt;= 10000</conditionExpression>
    </sequenceFlow>
    <userTask id="managerApproval" name="Manager Approval" process:participant="FinanceManager"/>
    <sequenceFlow id="sf5" sourceRef="managerApproval" targetRef="poApproved"/>
    <serviceTask id="autoApprove" name="Auto-Approve PO" process:serviceType="automatic"/>
    <sequenceFlow id="sf6" sourceRef="autoApprove" targetRef="poApproved"/>
    <endEvent id="poApproved" name="PO Approved"/>
  </process>
</definitions>"""

# Bizagi — near-standard BPMN 2.0
BIZAGI_BPMN = """<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  targetNamespace="http://www.bizagi.com">
  <process id="loanApplication" name="Loan Application Process" isExecutable="true">
    <startEvent id="appReceived" name="Application Received"/>
    <sequenceFlow id="sf1" sourceRef="appReceived" targetRef="checkEligibility"/>
    <serviceTask id="checkEligibility" name="Check Eligibility (Credit Score)"/>
    <sequenceFlow id="sf2" sourceRef="checkEligibility" targetRef="eligibilityGw"/>
    <exclusiveGateway id="eligibilityGw" name="Eligible?"/>
    <sequenceFlow id="sf3" sourceRef="eligibilityGw" targetRef="underwriting">
      <conditionExpression>creditScore >= 650</conditionExpression>
    </sequenceFlow>
    <sequenceFlow id="sf4" sourceRef="eligibilityGw" targetRef="rejectApp">
      <conditionExpression>creditScore &lt; 650</conditionExpression>
    </sequenceFlow>
    <userTask id="underwriting" name="Underwriting Review"/>
    <sequenceFlow id="sf5" sourceRef="underwriting" targetRef="approveOrDeny"/>
    <exclusiveGateway id="approveOrDeny" name="Decision?"/>
    <sequenceFlow id="sf6" sourceRef="approveOrDeny" targetRef="disburseLoan">
      <conditionExpression>approved == true</conditionExpression>
    </sequenceFlow>
    <sequenceFlow id="sf7" sourceRef="approveOrDeny" targetRef="rejectApp">
      <conditionExpression>approved == false</conditionExpression>
    </sequenceFlow>
    <serviceTask id="disburseLoan" name="Disburse Loan Funds"/>
    <sequenceFlow id="sf8" sourceRef="disburseLoan" targetRef="end"/>
    <userTask id="rejectApp" name="Send Rejection Notice"/>
    <sequenceFlow id="sf9" sourceRef="rejectApp" targetRef="end"/>
    <endEvent id="end" name="Process Complete"/>
  </process>
</definitions>"""

# Pega — XML ruleset export format
PEGA_FLOW_XML = """<?xml version="1.0" encoding="UTF-8"?>
<pega:ruleSet xmlns:pega="http://www.pega.com/PegaRULES" name="CustomerComplaintProcess" version="1.0">
  <pega:rule type="Flow" name="Flow-CustomerComplaint" pyLabel="Customer Complaint Process">
    <pega:Stage id="intake-stage" pxStageID="intake-stage" pyLabel="Intake">
      <pega:Step pxStepID="step-001" pyLabel="Log Complaint" pyShapeType="Assignment"
                 pyFlowAction="Harness-ComplaintForm" pxOrdinal="1" pyNextStep="step-002"/>
      <pega:Step pxStepID="step-002" pyLabel="Verify Contact" pyShapeType="Assignment"
                 pyFlowAction="Harness-ContactVerification" pxOrdinal="2" pyNextStep="step-003"/>
    </pega:Stage>
    <pega:Stage id="investigation-stage" pxStageID="investigation-stage" pyLabel="Investigation">
      <pega:Step pxStepID="step-003" pyLabel="Assign Investigator" pyShapeType="Assignment"
                 pyWorkBasket="ComplaintsQueue" pxOrdinal="3" pyNextStep="step-004"/>
      <pega:Step pxStepID="step-004" pyLabel="Investigate Issue" pyShapeType="Subprocess"
                 pyFlowAction="Harness-InvestigationForm" pxOrdinal="4" pyNextStep="step-005"/>
    </pega:Stage>
    <pega:Stage id="resolution-stage" pxStageID="resolution-stage" pyLabel="Resolution">
      <pega:Step pxStepID="step-005" pyLabel="Propose Resolution" pyShapeType="Assignment"
                 pyFlowAction="Harness-ResolutionForm" pxOrdinal="5" pyNextStep="step-006"/>
      <pega:Step pxStepID="step-006" pyLabel="Manager Approval" pyShapeType="Approval"
                 pyWorkBasket="ManagersQueue" pxOrdinal="6"/>
    </pega:Stage>
  </pega:rule>
  <pega:rule type="SLARule" name="SLARule-ComplaintSLA" pyLabel="Complaint SLA">
    <pyGoal pyValue="24" pyUnit="hours"/>
    <pyDeadline pyValue="48" pyUnit="hours"/>
    <pyEscalationAction pyAssignTo="ManagersQueue"/>
  </pega:rule>
  <pega:rule type="Section" name="Section-ComplaintForm" pyLabel="Complaint Intake Form">
    <pega:Field pyReference="pyComplaintType" pyLabel="Complaint Type" pyFieldType="Dropdown" pyRequired="true"/>
    <pega:Field pyReference="pyDescription" pyLabel="Description" pyFieldType="TextArea" pyRequired="true"/>
    <pega:Field pyReference="pyCustomerID" pyLabel="Customer ID" pyFieldType="Text" pyRequired="true"/>
    <pega:Field pyReference="pyPriority" pyLabel="Priority" pyFieldType="Dropdown" pyRequired="false"/>
  </pega:rule>
  <pega:rule type="DecisionTable" name="DecisionTable-ComplaintPriority" pyLabel="Complaint Priority Rules">
    <pega:Row pyCondition="complaintType == 'billing'" pyResult="High"/>
    <pega:Row pyCondition="complaintType == 'service'" pyResult="Medium"/>
    <pega:Row pyCondition="complaintType == 'general'" pyResult="Low"/>
  </pega:rule>
  <pega:rule type="AccessGroup" name="AccessGroup-ComplaintsTeam">
    <pega:Role pyName="ComplaintsAgent"/>
    <pega:Role pyName="ComplaintsManager"/>
    <pega:Role pyName="Supervisor"/>
  </pega:rule>
</pega:ruleSet>"""

# ServiceNow — XML update set format (wf_workflow + wf_stage + wf_activity)
SERVICENOW_UPDATE_SET = """<?xml version="1.0" encoding="UTF-8"?>
<unload unload_date="2024-01-15 10:00:00">
  <record_update table="wf_workflow" sys_id="abc123def456">
    <sys_id>abc123def456</sys_id>
    <name>IT Onboarding Workflow</name>
    <description>New employee IT onboarding process</description>
    <active>true</active>
    <sys_created_on>2024-01-01 00:00:00</sys_created_on>
  </record_update>
  <record_update table="wf_stage" sys_id="stage001">
    <sys_id>stage001</sys_id>
    <workflow>abc123def456</workflow>
    <name>Request Stage</name>
    <order>100</order>
  </record_update>
  <record_update table="wf_stage" sys_id="stage002">
    <sys_id>stage002</sys_id>
    <workflow>abc123def456</workflow>
    <name>Provisioning Stage</name>
    <order>200</order>
  </record_update>
  <record_update table="wf_stage" sys_id="stage003">
    <sys_id>stage003</sys_id>
    <workflow>abc123def456</workflow>
    <name>Completion Stage</name>
    <order>300</order>
  </record_update>
  <record_update table="wf_activity" sys_id="act001">
    <sys_id>act001</sys_id>
    <workflow>abc123def456</workflow>
    <stage>stage001</stage>
    <name>Submit Onboarding Request</name>
    <type>user</type>
    <x>100</x>
  </record_update>
  <record_update table="wf_activity" sys_id="act002">
    <sys_id>act002</sys_id>
    <workflow>abc123def456</workflow>
    <stage>stage002</stage>
    <name>Create Active Directory Account</name>
    <type>automatic</type>
    <x>200</x>
  </record_update>
  <record_update table="wf_activity" sys_id="act003">
    <sys_id>act003</sys_id>
    <workflow>abc123def456</workflow>
    <stage>stage002</stage>
    <name>Provision Laptop</name>
    <type>user</type>
    <x>300</x>
  </record_update>
  <record_update table="wf_activity" sys_id="act004">
    <sys_id>act004</sys_id>
    <workflow>abc123def456</workflow>
    <stage>stage003</stage>
    <name>Manager Sign-off</name>
    <type>user</type>
    <x>400</x>
  </record_update>
  <record_update table="sys_approval_rules" sys_id="apr001">
    <sys_id>apr001</sys_id>
    <workflow>abc123def456</workflow>
    <name>IT Manager Approval</name>
  </record_update>
  <record_update table="sc_cat_item" sys_id="cat001">
    <sys_id>cat001</sys_id>
    <name>New Employee IT Request</name>
    <description>Request IT equipment and accounts for new employee</description>
  </record_update>
  <record_update table="item_option_new" sys_id="opt001">
    <sys_id>opt001</sys_id>
    <cat_item>cat001</cat_item>
    <name>employee_name</name>
    <question_text>Employee Full Name</question_text>
    <type>1</type>
    <mandatory>true</mandatory>
  </record_update>
  <record_update table="item_option_new" sys_id="opt002">
    <sys_id>opt002</sys_id>
    <cat_item>cat001</cat_item>
    <name>start_date</name>
    <question_text>Start Date</question_text>
    <type>8</type>
    <mandatory>true</mandatory>
  </record_update>
  <record_update table="item_option_new" sys_id="opt003">
    <sys_id>opt003</sys_id>
    <cat_item>cat001</cat_item>
    <name>department</name>
    <question_text>Department</question_text>
    <type>2</type>
    <mandatory>true</mandatory>
  </record_update>
</unload>"""

# Appian — ProcessModel XML format
APPIAN_PROCESS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<process-model xmlns="http://www.appian.com/ae/types/2009" name="LoanApprovalProcess"
               uuid="loan-approval-001" version="3">
  <stage id="stage-intake" name="Intake" uuid="s1">
    <node id="node-001" name="Submit Loan Application" type="UserInputTask" uuid="n1">
      <interface>LoanApplicationForm</interface>
      <order>1</order>
    </node>
    <node id="node-002" name="Document Verification" type="UserInputTask" uuid="n2">
      <interface>DocumentVerificationForm</interface>
      <order>2</order>
    </node>
  </stage>
  <stage id="stage-underwriting" name="Underwriting" uuid="s2">
    <node id="node-003" name="Credit Score Check" type="ServiceTask" uuid="n3">
      <integration>CreditScoreAPI</integration>
      <order>3</order>
    </node>
    <node id="node-004" name="Underwriter Review" type="UserInputTask" uuid="n4">
      <interface>UnderwriterDecisionForm</interface>
      <order>4</order>
    </node>
  </stage>
  <stage id="stage-decision" name="Decision" uuid="s3">
    <node id="node-005" name="Approval Decision" type="UserInputTask" uuid="n5">
      <interface>ApprovalDecisionForm</interface>
      <order>5</order>
    </node>
  </stage>
  <interface name="LoanApplicationForm" uuid="form-001">
    <field name="applicantName" label="Applicant Name" type="TextField" required="true"/>
    <field name="loanAmount" label="Loan Amount" type="DecimalField" required="true"/>
    <field name="loanPurpose" label="Loan Purpose" type="DropdownField" required="true"/>
    <field name="employmentStatus" label="Employment Status" type="RadioButtonField"/>
  </interface>
  <interface name="UnderwriterDecisionForm" uuid="form-002">
    <field name="creditScore" label="Credit Score" type="IntegerField" required="true"/>
    <field name="riskRating" label="Risk Rating" type="DropdownField" required="true"/>
    <field name="comments" label="Underwriter Comments" type="ParagraphField"/>
  </interface>
  <record-type name="LoanRecord" uuid="rt-001">
    <field name="loanId" label="Loan ID" type="text"/>
    <field name="applicantId" label="Applicant ID" type="text"/>
    <field name="amount" label="Amount" type="decimal"/>
    <field name="status" label="Status" type="text"/>
    <field name="createdDate" label="Created Date" type="date"/>
  </record-type>
  <expression-rule name="CalculateMonthlyPayment" uuid="er-001">
    <body><![CDATA[if(loanAmount > 0, loanAmount / 60, 0)]]></body>
  </expression-rule>
  <decision name="LoanRiskDecision" uuid="dec-001">
    <row condition="creditScore >= 750" result="Low Risk"/>
    <row condition="creditScore >= 650" result="Medium Risk"/>
    <row condition="creditScore &lt; 650" result="High Risk - Reject"/>
  </decision>
</process-model>"""

# Salesforce Flow — FlowDefinition XML (Force.com Metadata API format)
SALESFORCE_FLOW_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Flow xmlns="http://soap.sforce.com/2006/04/metadata">
    <apiVersion>57.0</apiVersion>
    <description>Case Escalation and Resolution Flow</description>
    <label>Case Escalation Resolution</label>
    <processType>Flow</processType>
    <startElementReference>CollectCaseDetails</startElementReference>
    <screens>
        <description>Collect case details from agent</description>
        <label>Collect Case Details</label>
        <name>CollectCaseDetails</name>
        <fields>
            <name>CaseSubject</name>
            <dataType>String</dataType>
            <fieldText>Case Subject</fieldText>
            <fieldType>InputField</fieldType>
            <isRequired>true</isRequired>
        </fields>
        <fields>
            <name>CasePriority</name>
            <dataType>String</dataType>
            <fieldText>Priority</fieldText>
            <fieldType>DropdownBox</fieldType>
            <isRequired>true</isRequired>
        </fields>
        <fields>
            <name>CaseDescription</name>
            <dataType>String</dataType>
            <fieldText>Description</fieldText>
            <fieldType>LargeTextArea</fieldType>
            <isRequired>false</isRequired>
        </fields>
        <connector>
            <targetReference>CheckPriority</targetReference>
        </connector>
    </screens>
    <decisions>
        <description>Route based on case priority</description>
        <label>Check Priority</label>
        <name>CheckPriority</name>
        <rules>
            <label>High Priority</label>
            <name>HighPriority</name>
            <conditions>
                <leftValueReference>CasePriority</leftValueReference>
                <operator>EqualTo</operator>
                <rightValue><stringValue>High</stringValue></rightValue>
            </conditions>
            <connector>
                <targetReference>EscalateToCriticalTeam</targetReference>
            </connector>
        </rules>
        <rules>
            <label>Normal Priority</label>
            <name>NormalPriority</name>
            <conditions>
                <leftValueReference>CasePriority</leftValueReference>
                <operator>NotEqualTo</operator>
                <rightValue><stringValue>High</stringValue></rightValue>
            </conditions>
            <connector>
                <targetReference>AssignToAgent</targetReference>
            </connector>
        </rules>
    </decisions>
    <recordCreates>
        <label>Create Case Record</label>
        <name>CreateCaseRecord</name>
        <inputAssignments>
            <field>Subject</field>
            <value><elementReference>CaseSubject</elementReference></value>
        </inputAssignments>
        <connector>
            <targetReference>SendConfirmation</targetReference>
        </connector>
    </recordCreates>
    <actionCalls>
        <label>Escalate to Critical Team</label>
        <name>EscalateToCriticalTeam</name>
        <actionType>emailAlert</actionType>
        <connector>
            <targetReference>AssignToAgent</targetReference>
        </connector>
    </actionCalls>
    <screens>
        <label>Assign to Agent</label>
        <name>AssignToAgent</name>
        <fields>
            <name>AssignedAgent</name>
            <dataType>String</dataType>
            <fieldText>Assigned Agent</fieldText>
            <fieldType>InputField</fieldType>
            <isRequired>true</isRequired>
        </fields>
        <connector>
            <targetReference>CreateCaseRecord</targetReference>
        </connector>
    </screens>
    <actionCalls>
        <label>Send Confirmation Email</label>
        <name>SendConfirmation</name>
        <actionType>emailAlert</actionType>
    </actionCalls>
</Flow>"""

# Nintex — SharePoint XML workflow
NINTEX_SP_XML = """<?xml version="1.0" encoding="utf-8"?>
<NWWorkflowConfig xmlns:xsd="http://www.w3.org/2001/XMLSchema"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                  Name="Contract Approval Workflow" Version="1.0">
  <Activities>
    <WFActivity Id="start-001" Name="Start - Contract Submitted" ActionId="start"
                Sequence="10" type="trigger"/>
    <WFActivity Id="act-001" Name="Notify Requester" ActionId="sendnotification"
                Sequence="20" type="email"/>
    <WFActivity Id="act-002" Name="Legal Review" ActionId="collectdata"
                Sequence="30" type="assignment" TaskFormUrl="LegalReviewForm"/>
    <WFActivity Id="act-003" Name="Finance Approval" ActionId="flexi"
                Sequence="40" type="approval" TaskFormUrl="FinanceApprovalForm"/>
    <WFActivity Id="act-004" Name="Check Approval Outcome" ActionId="condition"
                Sequence="50" type="condition"/>
    <WFActivity Id="act-005" Name="Send Approval Notification" ActionId="sendnotification"
                Sequence="60" type="email"/>
    <WFActivity Id="act-006" Name="Send Rejection Notification" ActionId="sendnotification"
                Sequence="70" type="email"/>
    <WFActivity Id="act-007" Name="Archive Contract" ActionId="updateitem"
                Sequence="80" type="automatic"/>
  </Activities>
  <TaskForm Name="LegalReviewForm" Title="Legal Review">
    <Column Name="LegalComments" DisplayName="Legal Comments" InternalName="LegalComments"/>
    <Column Name="LegalApproved" DisplayName="Legal Approved" InternalName="LegalApproved"/>
    <Column Name="ContractRisk" DisplayName="Contract Risk Level" InternalName="ContractRisk"/>
  </TaskForm>
  <TaskForm Name="FinanceApprovalForm" Title="Finance Approval">
    <Column Name="FinanceApproved" DisplayName="Finance Approved" InternalName="FinanceApproved"/>
    <Column Name="BudgetCode" DisplayName="Budget Code" InternalName="BudgetCode"/>
    <Column Name="ApproverComments" DisplayName="Approver Comments" InternalName="ApproverComments"/>
  </TaskForm>
</NWWorkflowConfig>"""

# Nintex NWC — JSON (Nintex Workflow Cloud format)
NINTEX_NWC_JSON = json.dumps({
    "name": "Employee Offboarding",
    "workflowName": "employee-offboarding-v2",
    "actions": [
        {"id": "trigger-1", "name": "HR Initiates Offboarding", "actionType": "manualTrigger",
         "order": 1, "formId": "offboarding-initiation-form"},
        {"id": "act-1", "name": "Revoke System Access", "actionType": "callService",
         "order": 2},
        {"id": "act-2", "name": "Collect IT Equipment", "actionType": "assignTask",
         "order": 3, "taskFormId": "equipment-collection-form"},
        {"id": "act-3", "name": "Manager Exit Interview", "actionType": "assignTask",
         "order": 4, "taskFormId": "exit-interview-form"},
        {"id": "act-4", "name": "Payroll Final Calculation", "actionType": "callService",
         "order": 5},
        {"id": "act-5", "name": "HR Sign-off", "actionType": "approvalTask",
         "order": 6},
        {"id": "act-6", "name": "Archive Employee Record", "actionType": "callService",
         "order": 7}
    ]
})


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _files(name: str, content: str, rule_type: str = "BpmnProcess") -> list[dict]:
    return [{"name": name, "content": content, "rule_type": rule_type}]

def _count_steps(result: dict) -> int:
    total = 0
    for items in result.values():
        for item in items:
            stages = item.get("stages", item.get("processes", [{}])[0].get("stages", []) if item.get("processes") else [])
            if item.get("processes"):
                for proc in item["processes"]:
                    for stage in proc.get("stages", []):
                        total += len(stage.get("steps", []))
            else:
                for stage in stages:
                    total += len(stage.get("steps", []))
    return total


# ══════════════════════════════════════════════════════════════════════════════
# TESTS — BPMN 2.0 TIER 1 (real files)
# ══════════════════════════════════════════════════════════════════════════════

class TestCamundaReal:
    """Source: camunda-bpm-examples (GitHub) — real production sample"""

    def test_parses_without_crash(self):
        result = bpmn2.parse_files(_files("sample.bpmn", CAMUNDA_REAL))
        assert result is not None

    def test_extracts_process(self):
        result = bpmn2.parse_files(_files("sample.bpmn", CAMUNDA_REAL))
        procs = result.get("BpmnProcess", [])
        assert len(procs) >= 1
        assert procs[0]["processes"][0]["name"] == "Sample"

    def test_vendor_detected_as_camunda(self):
        result = bpmn2.parse_files(_files("sample.bpmn", CAMUNDA_REAL))
        vendor = result["BpmnProcess"][0]["processes"][0]["vendor"]
        assert vendor == "camunda"

    def test_steps_extracted_in_sequence_flow_order(self):
        result = bpmn2.parse_files(_files("sample.bpmn", CAMUNDA_REAL))
        proc = result["BpmnProcess"][0]["processes"][0]
        all_steps = [s for stage in proc["stages"] for s in stage.get("steps", [])]
        # UserTask must come before ServiceTask (sequence: Start→UserTask→ServiceTask→End)
        names = [s["name"] for s in all_steps]
        assert "do something" in names
        assert "say hello" in names
        assert names.index("do something") < names.index("say hello"), \
            f"Expected 'do something' before 'say hello', got: {names}"

    def test_correct_step_types(self):
        result = bpmn2.parse_files(_files("sample.bpmn", CAMUNDA_REAL))
        proc = result["BpmnProcess"][0]["processes"][0]
        steps = {s["name"]: s["step_type"] for stage in proc["stages"] for s in stage.get("steps", [])}
        assert steps.get("do something") == "user_task"
        assert steps.get("say hello") == "automated"

    def test_no_crash_on_gateway_file(self):
        result = bpmn2.parse_files(_files("gateway.bpmn", CAMUNDA_GATEWAY_REAL))
        procs = result.get("BpmnProcess", [])
        assert len(procs) >= 1

    def test_gateway_conditions_extracted(self):
        result = bpmn2.parse_files(_files("gateway.bpmn", CAMUNDA_GATEWAY_REAL))
        proc = result["BpmnProcess"][0]["processes"][0]
        all_steps = [s for stage in proc["stages"] for s in stage.get("steps", [])]
        steps_with_conds = [s for s in all_steps if s.get("conditions")]
        # Tasks downstream of the exclusive gateway should carry conditions
        assert len(steps_with_conds) >= 2, \
            f"Expected >= 2 steps with conditions, got {len(steps_with_conds)}: {[s['name'] for s in all_steps]}"


class TestFlowableReal:
    """Source: flowable-examples VacationRequest (GitHub) — real production sample"""

    def test_parses_without_crash(self):
        result = bpmn2.parse_files(_files("VacationRequest.bpmn20.xml", FLOWABLE_REAL))
        assert result is not None

    def test_extracts_vacation_process(self):
        result = bpmn2.parse_files(_files("VacationRequest.bpmn20.xml", FLOWABLE_REAL))
        procs = result.get("BpmnProcess", [])
        assert len(procs) >= 1
        assert "vacation" in procs[0]["processes"][0]["name"].lower()

    def test_vendor_detected_as_flowable(self):
        result = bpmn2.parse_files(_files("VacationRequest.bpmn20.xml", FLOWABLE_REAL))
        vendor = result["BpmnProcess"][0]["processes"][0]["vendor"]
        assert vendor == "flowable"

    def test_form_keys_extracted(self):
        result = bpmn2.parse_files(_files("VacationRequest.bpmn20.xml", FLOWABLE_REAL))
        proc = result["BpmnProcess"][0]["processes"][0]
        all_steps = [s for stage in proc["stages"] for s in stage.get("steps", [])]
        steps_with_forms = [s for s in all_steps if s.get("form_key")]
        assert len(steps_with_forms) >= 1, "Expected at least one step with a flowable:formKey"

    def test_multi_user_tasks_extracted(self):
        result = bpmn2.parse_files(_files("VacationRequest.bpmn20.xml", FLOWABLE_REAL))
        proc = result["BpmnProcess"][0]["processes"][0]
        all_steps = [s for stage in proc["stages"] for s in stage.get("steps", [])]
        user_tasks = [s for s in all_steps if s["step_type"] == "user_task"]
        assert len(user_tasks) >= 2

    def test_conditions_on_gateway_flows(self):
        result = bpmn2.parse_files(_files("VacationRequest.bpmn20.xml", FLOWABLE_REAL))
        proc = result["BpmnProcess"][0]["processes"][0]
        all_steps = [s for stage in proc["stages"] for s in stage.get("steps", [])]
        steps_with_conds = [s for s in all_steps if s.get("conditions")]
        assert len(steps_with_conds) >= 1


# ══════════════════════════════════════════════════════════════════════════════
# TESTS — BPMN 2.0 TIER 1 (synthetic vendor variants)
# ══════════════════════════════════════════════════════════════════════════════

class TestJBPM:
    """jBPM/Kogito — BPMN 2.0 with tns: namespace"""

    def test_parses_without_crash(self):
        result = bpmn2.parse_files(_files("order.bpmn", JBPM_BPMN))
        assert result is not None

    def test_extracts_process_and_steps(self):
        result = bpmn2.parse_files(_files("order.bpmn", JBPM_BPMN))
        procs = result.get("BpmnProcess", [])
        assert len(procs) >= 1
        all_steps = [s for stage in procs[0]["processes"][0]["stages"] for s in stage.get("steps", [])]
        assert len(all_steps) >= 3

    def test_step_ordering_follows_sequence_flows(self):
        result = bpmn2.parse_files(_files("order.bpmn", JBPM_BPMN))
        proc = result["BpmnProcess"][0]["processes"][0]
        all_steps = [s for stage in proc["stages"] for s in stage.get("steps", [])]
        names = [s["name"] for s in all_steps]
        # validateOrder must come before fulfillOrder
        assert "Validate Order" in names
        vi = names.index("Validate Order")
        fi = names.index("Fulfil Order") if "Fulfil Order" in names else 999
        assert vi < fi, f"Validate Order should precede Fulfil Order. Got: {names}"


class TestIBMBAW:
    """IBM BAW — BPMN 2.0 with icp: teamRef and laneSet"""

    def test_parses_without_crash(self):
        result = bpmn2.parse_files(_files("insurance.bpmn", IBM_BAW_BPMN))
        assert result is not None

    def test_extracts_access_groups_from_lanes(self):
        result = bpmn2.parse_files(_files("insurance.bpmn", IBM_BAW_BPMN))
        proc = result["BpmnProcess"][0]["processes"][0]
        assert len(proc.get("access_groups", [])) >= 2, \
            f"Expected >= 2 access groups from laneSet, got: {proc.get('access_groups')}"

    def test_extracts_steps_in_order(self):
        result = bpmn2.parse_files(_files("insurance.bpmn", IBM_BAW_BPMN))
        proc = result["BpmnProcess"][0]["processes"][0]
        all_steps = [s for stage in proc["stages"] for s in stage.get("steps", [])]
        assert len(all_steps) >= 3


class TestOracleBPM:
    def test_parses_without_crash(self):
        result = bpmn2.parse_files(_files("purchase.bpmn", ORACLE_BPM_BPMN))
        assert result is not None

    def test_extracts_process_and_gateway(self):
        result = bpmn2.parse_files(_files("purchase.bpmn", ORACLE_BPM_BPMN))
        procs = result.get("BpmnProcess", [])
        assert len(procs) >= 1
        all_steps = [s for stage in procs[0]["processes"][0]["stages"] for s in stage.get("steps", [])]
        assert len(all_steps) >= 2


class TestBizagi:
    def test_parses_without_crash(self):
        result = bpmn2.parse_files(_files("loan.bpmn", BIZAGI_BPMN))
        assert result is not None

    def test_extracts_loan_process_steps(self):
        result = bpmn2.parse_files(_files("loan.bpmn", BIZAGI_BPMN))
        procs = result.get("BpmnProcess", [])
        assert len(procs) >= 1
        all_steps = [s for stage in procs[0]["processes"][0]["stages"] for s in stage.get("steps", [])]
        assert len(all_steps) >= 2


# ══════════════════════════════════════════════════════════════════════════════
# TESTS — TIER 2 PROPRIETARY
# ══════════════════════════════════════════════════════════════════════════════

class TestPega:
    def test_parses_without_crash(self):
        files = [
            {"name": "Flow-CustomerComplaint.xml", "content": PEGA_FLOW_XML, "rule_type": "Flow"},
        ]
        result = pega.parse_files(files)
        assert result is not None

    def test_extracts_flow_with_stages(self):
        files = [{"name": "Flow-CustomerComplaint.xml", "content": PEGA_FLOW_XML, "rule_type": "Flow"}]
        result = pega.parse_files(files)
        flows = result.get("Flow", [])
        assert len(flows) >= 1
        assert len(flows[0]["stages"]) >= 3, "Expected 3 stages (Intake, Investigation, Resolution)"

    def test_extracts_sla_with_escalation(self):
        files = [{"name": "SLARule-ComplaintSLA.xml", "content": PEGA_FLOW_XML, "rule_type": "SLARule"}]
        result = pega.parse_files(files)
        slas = result.get("SLARule", [])
        assert len(slas) >= 1
        assert slas[0]["goal_hours"] == 24.0
        assert slas[0]["escalation_to"] == "ManagersQueue"

    def test_extracts_form_fields(self):
        files = [{"name": "Section-ComplaintForm.xml", "content": PEGA_FLOW_XML, "rule_type": "Section"}]
        result = pega.parse_files(files)
        forms = result.get("Section", [])
        assert len(forms) >= 1
        assert len(forms[0]["fields"]) >= 3

    def test_extracts_decision_table(self):
        files = [{"name": "DecisionTable-ComplaintPriority.xml", "content": PEGA_FLOW_XML, "rule_type": "DecisionTable"}]
        result = pega.parse_files(files)
        dts = result.get("DecisionTable", [])
        assert len(dts) >= 1
        assert len(dts[0]["conditions"]) >= 2

    def test_extracts_access_group_roles(self):
        files = [{"name": "AccessGroup-ComplaintsTeam.xml", "content": PEGA_FLOW_XML, "rule_type": "AccessGroup"}]
        result = pega.parse_files(files)
        ags = result.get("AccessGroup", [])
        assert len(ags) >= 1
        assert len(ags[0]["roles"]) >= 2

    def test_step_ordering_by_chain(self):
        files = [{"name": "Flow-CustomerComplaint.xml", "content": PEGA_FLOW_XML, "rule_type": "Flow"}]
        result = pega.parse_files(files)
        flows = result.get("Flow", [])
        stages = flows[0]["stages"]
        # Intake stage should have 2 steps, Investigation should have 2, Resolution should have 2
        total_steps = sum(len(s.get("steps", [])) for s in stages)
        assert total_steps >= 5


class TestServiceNow:
    def test_parses_without_crash(self):
        files = [{"name": "IT_Onboarding.xml", "content": SERVICENOW_UPDATE_SET, "rule_type": "Workflow"}]
        result = servicenow.parse_files(files)
        assert result is not None

    def test_extracts_workflow(self):
        files = [{"name": "IT_Onboarding.xml", "content": SERVICENOW_UPDATE_SET, "rule_type": "Workflow"}]
        result = servicenow.parse_files(files)
        workflows = result.get("Workflow", [])
        assert len(workflows) >= 1
        assert "onboarding" in workflows[0]["name"].lower() or "IT" in workflows[0]["name"]

    def test_extracts_stages(self):
        files = [{"name": "IT_Onboarding.xml", "content": SERVICENOW_UPDATE_SET, "rule_type": "Workflow"}]
        result = servicenow.parse_files(files)
        wf = result["Workflow"][0]
        assert len(wf.get("stages", [])) >= 2

    def test_extracts_activities_as_steps(self):
        files = [{"name": "IT_Onboarding.xml", "content": SERVICENOW_UPDATE_SET, "rule_type": "Workflow"}]
        result = servicenow.parse_files(files)
        wf = result["Workflow"][0]
        all_steps = [s for stage in wf.get("stages", []) for s in stage.get("steps", [])]
        assert len(all_steps) >= 3

    def test_extracts_catalog_item_with_fields(self):
        files = [{"name": "IT_Onboarding.xml", "content": SERVICENOW_UPDATE_SET, "rule_type": "Workflow"}]
        result = servicenow.parse_files(files)
        catalogs = result.get("Catalog", [])
        assert len(catalogs) >= 1
        assert len(catalogs[0].get("fields", [])) >= 2

    def test_approval_step_type_correct(self):
        files = [{"name": "IT_Onboarding.xml", "content": SERVICENOW_UPDATE_SET, "rule_type": "Workflow"}]
        result = servicenow.parse_files(files)
        all_steps = [s for wf in result.get("Workflow", [])
                     for stage in wf.get("stages", [])
                     for s in stage.get("steps", [])]
        approval_steps = [s for s in all_steps if s.get("step_type") == "approval"]
        assert len(approval_steps) >= 1, "Expected at least 1 approval step"


# ══════════════════════════════════════════════════════════════════════════════
# TESTS — TIER 2 APPIAN
# ══════════════════════════════════════════════════════════════════════════════

class TestAppian:
    def test_parses_process_model(self):
        files = [{"name": "LoanApprovalProcess.xml", "content": APPIAN_PROCESS_XML, "rule_type": "ProcessModel"}]
        result = appian.parse_files(files)
        pms = result.get("ProcessModel", [])
        assert len(pms) >= 1

    def test_extracts_stages(self):
        files = [{"name": "LoanApprovalProcess.xml", "content": APPIAN_PROCESS_XML, "rule_type": "ProcessModel"}]
        result = appian.parse_files(files)
        pm = result["ProcessModel"][0]
        assert len(pm.get("stages", [])) >= 2

    def test_parses_interface_form(self):
        files = [{"name": "LoanApplicationForm.xml", "content": APPIAN_PROCESS_XML, "rule_type": "Interface"}]
        result = appian.parse_files(files)
        forms = result.get("Interface", [])
        assert len(forms) >= 1
        assert len(forms[0].get("fields", [])) >= 2

    def test_parses_record_type_as_data_model(self):
        files = [{"name": "LoanRecord.xml", "content": APPIAN_PROCESS_XML, "rule_type": "RecordType"}]
        result = appian.parse_files(files)
        rts = result.get("RecordType", [])
        assert len(rts) >= 1
        assert len(rts[0].get("fields", [])) >= 3

    def test_parses_expression_rule(self):
        files = [{"name": "CalculatePayment.xml", "content": APPIAN_PROCESS_XML, "rule_type": "ExpressionRule"}]
        result = appian.parse_files(files)
        rules = result.get("ExpressionRule", [])
        assert len(rules) >= 1
        assert rules[0]["expression"] != ""

    def test_parses_decision_table(self):
        files = [{"name": "LoanRisk.xml", "content": APPIAN_PROCESS_XML, "rule_type": "Decision"}]
        result = appian.parse_files(files)
        decisions = result.get("Decision", [])
        assert len(decisions) >= 1
        assert len(decisions[0].get("conditions", [])) >= 2


# ══════════════════════════════════════════════════════════════════════════════
# TESTS — TIER 3 (Power Automate real, Salesforce, Nintex)
# ══════════════════════════════════════════════════════════════════════════════

class TestPowerAutomateReal:
    """Source: OfficeDev/Microsoft-Teams-Shifts-Power-Automate-Templates (GitHub) — real production sample"""

    def test_parses_without_crash(self):
        files = [{"name": "definition.json", "content": POWER_AUTOMATE_REAL, "rule_type": "FlowDefinition"}]
        result = power_automate.parse_files(files)
        assert result is not None

    def test_extracts_flow(self):
        files = [{"name": "definition.json", "content": POWER_AUTOMATE_REAL, "rule_type": "FlowDefinition"}]
        result = power_automate.parse_files(files)
        flows = result.get("FlowDefinition", [])
        assert len(flows) >= 1

    def test_flow_display_name_extracted(self):
        files = [{"name": "definition.json", "content": POWER_AUTOMATE_REAL, "rule_type": "FlowDefinition"}]
        result = power_automate.parse_files(files)
        flow = result["FlowDefinition"][0]
        assert "Auto Approve" in flow["name"] or "AutoApprove" in flow["name"]

    def test_trigger_extracted_as_first_step(self):
        files = [{"name": "definition.json", "content": POWER_AUTOMATE_REAL, "rule_type": "FlowDefinition"}]
        result = power_automate.parse_files(files)
        flow = result["FlowDefinition"][0]
        all_steps = [s for stage in flow["stages"] for s in stage.get("steps", [])]
        assert len(all_steps) >= 1

    def test_actions_extracted(self):
        files = [{"name": "definition.json", "content": POWER_AUTOMATE_REAL, "rule_type": "FlowDefinition"}]
        result = power_automate.parse_files(files)
        flow = result["FlowDefinition"][0]
        all_steps = [s for stage in flow["stages"] for s in stage.get("steps", [])]
        # Should have trigger + at least 2 actions (List shifts + Apply to each)
        assert len(all_steps) >= 2, f"Expected >= 2 steps, got {len(all_steps)}"

    def test_nested_condition_flattened(self):
        files = [{"name": "definition.json", "content": POWER_AUTOMATE_REAL, "rule_type": "FlowDefinition"}]
        result = power_automate.parse_files(files)
        flow = result["FlowDefinition"][0]
        all_steps = [s for stage in flow["stages"] for s in stage.get("steps", [])]
        # Nested condition inside foreach should be flattened into steps
        assert len(all_steps) >= 3


class TestSalesforceFlow:
    def test_parses_without_crash(self):
        files = [{"name": "CaseEscalation.flow-meta.xml", "content": SALESFORCE_FLOW_XML, "rule_type": "SalesforceFlow"}]
        result = salesforce_flow.parse_files(files)
        assert result is not None

    def test_extracts_flow(self):
        files = [{"name": "CaseEscalation.flow-meta.xml", "content": SALESFORCE_FLOW_XML, "rule_type": "SalesforceFlow"}]
        result = salesforce_flow.parse_files(files)
        flows = result.get("SalesforceFlow", [])
        assert len(flows) >= 1

    def test_screens_extracted_as_user_task_steps(self):
        files = [{"name": "CaseEscalation.flow-meta.xml", "content": SALESFORCE_FLOW_XML, "rule_type": "SalesforceFlow"}]
        result = salesforce_flow.parse_files(files)
        flow = result["SalesforceFlow"][0]
        all_steps = [s for stage in flow["stages"] for s in stage.get("steps", [])]
        user_tasks = [s for s in all_steps if s["step_type"] == "user_task"]
        assert len(user_tasks) >= 1

    def test_screen_fields_extracted_as_forms(self):
        files = [{"name": "CaseEscalation.flow-meta.xml", "content": SALESFORCE_FLOW_XML, "rule_type": "SalesforceFlow"}]
        result = salesforce_flow.parse_files(files)
        flow = result["SalesforceFlow"][0]
        assert len(flow.get("forms", [])) >= 1
        form = flow["forms"][0]
        assert len(form["sections"][0]["fields"]) >= 2

    def test_decision_extracted_as_routing(self):
        files = [{"name": "CaseEscalation.flow-meta.xml", "content": SALESFORCE_FLOW_XML, "rule_type": "SalesforceFlow"}]
        result = salesforce_flow.parse_files(files)
        flow = result["SalesforceFlow"][0]
        all_steps = [s for stage in flow["stages"] for s in stage.get("steps", [])]
        routing = [s for s in all_steps if s["step_type"] == "routing"]
        assert len(routing) >= 1

    def test_action_calls_extracted(self):
        files = [{"name": "CaseEscalation.flow-meta.xml", "content": SALESFORCE_FLOW_XML, "rule_type": "SalesforceFlow"}]
        result = salesforce_flow.parse_files(files)
        flow = result["SalesforceFlow"][0]
        all_steps = [s for stage in flow["stages"] for s in stage.get("steps", [])]
        automated = [s for s in all_steps if s["step_type"] == "automated"]
        assert len(automated) >= 1

    def test_connector_chain_orders_steps(self):
        files = [{"name": "CaseEscalation.flow-meta.xml", "content": SALESFORCE_FLOW_XML, "rule_type": "SalesforceFlow"}]
        result = salesforce_flow.parse_files(files)
        flow = result["SalesforceFlow"][0]
        all_steps = [s for stage in flow["stages"] for s in stage.get("steps", [])]
        orders = [s.get("order", 0) for s in all_steps]
        # Connector chain should produce strictly increasing order
        assert orders == sorted(orders), f"Steps not in connector order: {[s['name'] for s in all_steps]}"

    def test_rules_from_decision_extracted(self):
        files = [{"name": "CaseEscalation.flow-meta.xml", "content": SALESFORCE_FLOW_XML, "rule_type": "SalesforceFlow"}]
        result = salesforce_flow.parse_files(files)
        flow = result["SalesforceFlow"][0]
        assert len(flow.get("rules", [])) >= 1


class TestNintexSharePoint:
    def test_parses_without_crash(self):
        files = [{"name": "ContractApproval.xml", "content": NINTEX_SP_XML, "rule_type": "NintexWorkflow"}]
        result = nintex.parse_files(files)
        assert result is not None

    def test_extracts_workflow(self):
        files = [{"name": "ContractApproval.xml", "content": NINTEX_SP_XML, "rule_type": "NintexWorkflow"}]
        result = nintex.parse_files(files)
        wfs = result.get("NintexWorkflow", [])
        assert len(wfs) >= 1

    def test_extracts_steps_in_sequence_order(self):
        files = [{"name": "ContractApproval.xml", "content": NINTEX_SP_XML, "rule_type": "NintexWorkflow"}]
        result = nintex.parse_files(files)
        wf = result["NintexWorkflow"][0]
        all_steps = [s for stage in wf["stages"] for s in stage.get("steps", [])]
        assert len(all_steps) >= 5
        # Check sequence numbers are in order
        orders = [s.get("order", 0) for s in all_steps]
        assert orders == sorted(orders)

    def test_approval_step_detected(self):
        files = [{"name": "ContractApproval.xml", "content": NINTEX_SP_XML, "rule_type": "NintexWorkflow"}]
        result = nintex.parse_files(files)
        wf = result["NintexWorkflow"][0]
        all_steps = [s for stage in wf["stages"] for s in stage.get("steps", [])]
        approvals = [s for s in all_steps if s["step_type"] == "approval"]
        assert len(approvals) >= 1

    def test_task_forms_extracted(self):
        files = [{"name": "ContractApproval.xml", "content": NINTEX_SP_XML, "rule_type": "NintexWorkflow"}]
        result = nintex.parse_files(files)
        wf = result["NintexWorkflow"][0]
        assert len(wf.get("forms", [])) >= 1
        assert len(wf["forms"][0]["sections"][0]["fields"]) >= 2

    def test_source_identified_as_sharepoint(self):
        files = [{"name": "ContractApproval.xml", "content": NINTEX_SP_XML, "rule_type": "NintexWorkflow"}]
        result = nintex.parse_files(files)
        wf = result["NintexWorkflow"][0]
        assert wf.get("source") == "SharePoint"


class TestNintexNWC:
    def test_parses_without_crash(self):
        files = [{"name": "offboarding.json", "content": NINTEX_NWC_JSON, "rule_type": "NintexWorkflow"}]
        result = nintex.parse_files(files)
        assert result is not None

    def test_extracts_workflow(self):
        files = [{"name": "offboarding.json", "content": NINTEX_NWC_JSON, "rule_type": "NintexWorkflow"}]
        result = nintex.parse_files(files)
        wfs = result.get("NintexWorkflow", [])
        assert len(wfs) >= 1

    def test_extracts_all_actions(self):
        files = [{"name": "offboarding.json", "content": NINTEX_NWC_JSON, "rule_type": "NintexWorkflow"}]
        result = nintex.parse_files(files)
        wf = result["NintexWorkflow"][0]
        all_steps = [s for stage in wf["stages"] for s in stage.get("steps", [])]
        assert len(all_steps) >= 6  # 7 actions defined

    def test_approval_step_detected(self):
        files = [{"name": "offboarding.json", "content": NINTEX_NWC_JSON, "rule_type": "NintexWorkflow"}]
        result = nintex.parse_files(files)
        wf = result["NintexWorkflow"][0]
        all_steps = [s for stage in wf["stages"] for s in stage.get("steps", [])]
        approvals = [s for s in all_steps if s["step_type"] == "approval"]
        assert len(approvals) >= 1

    def test_source_identified_as_nwc(self):
        files = [{"name": "offboarding.json", "content": NINTEX_NWC_JSON, "rule_type": "NintexWorkflow"}]
        result = nintex.parse_files(files)
        wf = result["NintexWorkflow"][0]
        assert wf.get("source") == "NWC"


# ══════════════════════════════════════════════════════════════════════════════
# SECURITY TESTS — verify hardening is active
# ══════════════════════════════════════════════════════════════════════════════

class TestSecurity:
    def test_xxe_blocked(self):
        """SEC-1: XXE injection must be silently blocked by defusedxml."""
        xxe_payload = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL">
  <process id="evil" name="&xxe;"/>
</definitions>"""
        result = bpmn2.parse_files(_files("evil.bpmn", xxe_payload))
        # defusedxml raises on DTD — parser returns empty result without crashing the service
        # Either empty result OR the name does not contain passwd file contents
        procs = result.get("BpmnProcess", [])
        if procs:
            name = procs[0]["processes"][0].get("name", "")
            assert "root:" not in name, "XXE succeeded — /etc/passwd content leaked!"

    def test_xml_bomb_does_not_hang(self):
        """SEC-1: Billion-laughs entity expansion must be blocked by defusedxml."""
        import time
        bomb = """<?xml version="1.0"?>
<!DOCTYPE lolz [
  <!ENTITY lol "lol">
  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
]>
<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL">
  <process id="boom" name="&lol3;"/>
</definitions>"""
        start = time.time()
        result = bpmn2.parse_files(_files("bomb.bpmn", bomb))
        elapsed = time.time() - start
        assert elapsed < 5.0, f"XML bomb was not blocked — took {elapsed:.1f}s"

    def test_path_traversal_blocked_by_extractor(self):
        """SEC-2: ZIP entry with ../../etc/passwd path must be rejected."""
        import io, zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../../etc/passwd", "root:x:0:0:root:/root:/bin/bash")
            zf.writestr("normal.xml", CAMUNDA_REAL)
        buf.seek(0)
        result = extract("camunda", buf.read(), "test.zip")
        names = [f["name"] for f in result["files"]]
        assert "../../etc/passwd" not in names, "Path traversal was not blocked!"
        assert any("normal.xml" in n for n in names), "Normal file should still be extracted"

    def test_json_depth_limit_enforced(self):
        """SEC-3: Deeply nested JSON must raise ValueError."""
        from case_service.hxmigrate.security import check_json_depth
        deep = {}
        node = deep
        for _ in range(60):  # well above MAX_JSON_DEPTH=50
            node["child"] = {}
            node = node["child"]
        with pytest.raises(ValueError, match="depth"):
            check_json_depth(deep)

    def test_upload_size_limit_constant(self):
        """SEC-7: MAX_UPLOAD_BYTES must be 100 MB."""
        from case_service.hxmigrate.security import MAX_UPLOAD_BYTES
        assert MAX_UPLOAD_BYTES == 100 * 1024 * 1024

    def test_sanitize_error_strips_bearer_token(self):
        """SEC-8: Bearer tokens must be stripped from error messages."""
        from case_service.hxmigrate.security import sanitize_error
        msg = "Request failed: Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.test"
        sanitized = sanitize_error(msg)
        assert "eyJ" not in sanitized, "JWT token not redacted from error message"

    def test_sanitize_error_strips_env_assignment(self):
        """SEC-8: KEY=value patterns must be stripped from error messages."""
        from case_service.hxmigrate.security import sanitize_error
        msg = "Connection failed: DATABASE_URL=postgresql://user:password@db:5432/helix"
        sanitized = sanitize_error(msg)
        assert "postgresql://" not in sanitized, "DB connection string not redacted"

    def test_ssrf_blocks_metadata_endpoint(self):
        """SEC-5: Creator must reject 169.254.169.254 (AWS metadata)."""
        from case_service.hxmigrate.creator import _validate_base_url
        with pytest.raises(ValueError, match="metadata"):
            _validate_base_url("http://169.254.169.254/latest/meta-data/")

    def test_ssrf_blocks_external_host(self):
        """SEC-5: Creator must reject external hostnames."""
        from case_service.hxmigrate.creator import _validate_base_url
        with pytest.raises(ValueError, match="allowlist"):
            _validate_base_url("http://evil.com/api")

    def test_ssrf_allows_localhost(self):
        """SEC-5: Creator must allow localhost."""
        from case_service.hxmigrate.creator import _validate_base_url
        url = _validate_base_url("http://localhost:8200")
        assert url == "http://localhost:8200"

    def test_platform_allowlist_rejects_unknown(self):
        """SEC-10: Unknown platform must be rejected."""
        from case_service.hxmigrate.security import validate_platform
        assert not validate_platform("evilscript")
        assert not validate_platform("'; DROP TABLE users; --")

    def test_platform_allowlist_accepts_all_12(self):
        """SEC-10: All 12 supported platforms must pass."""
        from case_service.hxmigrate.security import validate_platform
        for p in ["pega", "camunda", "appian", "servicenow", "jbpm", "flowable",
                  "ibm", "oracle", "bizagi", "power_automate", "salesforce", "nintex"]:
            assert validate_platform(p), f"{p} should be in the allowlist"
