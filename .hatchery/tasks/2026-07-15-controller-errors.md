# Task: controller-errors

**Status**: in-progress
**Branch**: hatchery/controller-errors
**Created**: 2026-07-15 08:04

## Objective

If the controller suffers an error, we get an undesirable UX.

For example, if the user configures an invalid step name, they are greeted just with this when following:

2026-07-14 17:39:13.804     INFO seekr_chain Including code from path: /Users/lgrado/Code/tools/seekr-chain/examples/1_code_package
2026-07-14 17:39:13.807     INFO seekr_chain Total size: 465 B
2026-07-14 17:39:13.875     INFO seekr_chain Packaging assets from staging dir: /var/folders/sx/2j914y216694jyncwfq564gh0000gn/T/tmp5327z7ne
2026-07-14 17:39:13.886     INFO seekr_chain Uploading assets to s3://seekr-ml-taw/seekr-chain/jobs/ka/13c6/assets.tar.gz (20.2 KiB)
2026-07-14 17:39:14.980     INFO seekr_chain Uploaded workflow secrets (count=2)
2026-07-14 17:39:18.598     INFO seekr_chain Launched controller job: ka13c6

[17:39:23] FAILED  0:05  code-upload  ka13c6

Just a single FAILED on the controller, and the follow process exits.

However, The controller continues to restart, trying to submit, but keeps failing

ka13c6-g8vk8                                                                    0/1     Error              0          62s
ka13c6-f9b5z                                                                    0/1     Error              0          49s
ka13c6-klws7                                                                    0/1     Error              0          27s

but the user is unaware this is even happening.

The first controller gets an error like this:

Defaulted container "controller" out of: controller, chain-init (init)
[controller] loaded DAG with 1 steps: ['step_name']
[controller] error: permanent submit error for step='step_name' jobset='ka13c6-step_name-js': (422)
Reason: Unprocessable Entity
HTTP response headers: HTTPHeaderDict({'Audit-Id': 'f2cfb7f3-d407-4a94-be52-2ca0b311a732', 'Cache-Control': 'no-cache, private', 'Content-Type': 'application/json', 'X-Kubernetes-Pf-Flowschema-Uid': '59074b42-9c59-43a3-83ce-55fd1770bc07', 'X-Kubernetes-Pf-Prioritylevel-Uid': '50ec73ef-34dc-40a6-aa5c-a03e7b91a618', 'Date': 'Tue, 14 Jul 2026 22:39:20 GMT', 'Content-Length': '960'})
HTTP response body: {"kind":"Status","apiVersion":"v1","metadata":{},"status":"Failure","message":"JobSet.jobset.x-k8s.io \"ka13c6-step_name-js\" is invalid: metadata.name: Invalid value: \"ka13c6-step_name-js\": a lowercase RFC 1123 subdomain must consist of lower case alphanumeric characters, '-' or '.', and must start and end with an alphanumeric character (e.g. 'example.com', regex used for validation is '[a-z0-9]([-a-z0-9]*[a-z0-9])?(\\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*')","reason":"Invalid","details":{"name":"ka13c6-step_name-js","group":"jobset.x-k8s.io","kind":"JobSet","causes":[{"reason":"FieldValueInvalid","message":"Invalid value: \"ka13c6-step_name-js\": a lowercase RFC 1123 subdomain must consist of lower case alphanumeric characters, '-' or '.', and must start and end with an alphanumeric character (e.g. 'example.com', regex used for validation is '[a-z0-9]([-a-z0-9]*[a-z0-9])?(\\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*')","field":"metadata.name"}]},"code":422}

, marking FAILED
[controller] warning: could not emit event 'WorkflowFailed': (403)
Reason: Forbidden
HTTP response headers: HTTPHeaderDict({'Audit-Id': 'e27f063e-4d09-45f6-bbd0-5517faae6403', 'Cache-Control': 'no-cache, private', 'Content-Type': 'application/json', 'X-Content-Type-Options': 'nosniff', 'X-Kubernetes-Pf-Flowschema-Uid': '59074b42-9c59-43a3-83ce-55fd1770bc07', 'X-Kubernetes-Pf-Prioritylevel-Uid': '50ec73ef-34dc-40a6-aa5c-a03e7b91a618', 'Date': 'Tue, 14 Jul 2026 22:39:20 GMT', 'Content-Length': '345'})
HTTP response body: {"kind":"Status","apiVersion":"v1","metadata":{},"status":"Failure","message":"events is forbidden: User \"system:serviceaccount:argo-workflows:argo-workflow\" cannot create resource \"events\" in API group \"\" in the namespace \"argo-workflows\": . Opc-Request-Id: \u003cnil\u003e","reason":"Forbidden","details":{"kind":"events"},"code":403}


[controller] workflow FAILED — failed steps: ['step_name']

Restarts will then have logs like this:

Defaulted container "controller" out of: controller, chain-init (init)
[controller] loaded DAG with 1 steps: ['step_name']
[controller] restored phases from ConfigMap: ['step_name']
[controller] warning: could not emit event 'WorkflowFailed': (403)
Reason: Forbidden
HTTP response headers: HTTPHeaderDict({'Audit-Id': 'c07642fc-c8d4-4a7f-afe3-47f1339bfc60', 'Cache-Control': 'no-cache, private', 'Content-Type': 'application/json', 'X-Content-Type-Options': 'nosniff', 'X-Kubernetes-Pf-Flowschema-Uid': '59074b42-9c59-43a3-83ce-55fd1770bc07', 'X-Kubernetes-Pf-Prioritylevel-Uid': '50ec73ef-34dc-40a6-aa5c-a03e7b91a618', 'Date': 'Wed, 15 Jul 2026 13:02:13 GMT', 'Content-Length': '345'})
HTTP response body: {"kind":"Status","apiVersion":"v1","metadata":{},"status":"Failure","message":"events is forbidden: User \"system:serviceaccount:argo-workflows:argo-workflow\" cannot create resource \"events\" in API group \"\" in the namespace \"argo-workflows\": . Opc-Request-Id: \u003cnil\u003e","reason":"Forbidden","details":{"kind":"events"},"code":403}


[controller] workflow FAILED — failed steps: ['step_name']


Our job is to improve this flow.

1. When following, if the controller pod fails, we should NOT exit. Instead, we should continue following the next controller pod that appears
2. If the controller jobset itself fails perminantly, then we should fail
3. Consider how we can prevent the controller pod from just looping in the event of a misconfigured job
4. Surface informative error to the user, similar to what we do if an image fails to pull

## Agreed Plan

*(To be filled in after planning discussion)*

## Progress Log

*(Steps will appear here once the plan is agreed)*

## Summary

*(Fill in on completion — then remove Agreed Plan and Progress Log above.
Cover: key decisions made, patterns established, files changed, gotchas,
and anything a future agent working in this repo should know.)*
