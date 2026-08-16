#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { PigMonitorEcrStack } from "../lib/ecr-stack";
import { PigMonitorStack } from "../lib/pig-monitor-stack";

const app = new cdk.App();

const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: "eu-west-3",
};

// Deployed and pushed to before PigMonitorStack -- see infra/README.md.
new PigMonitorEcrStack(app, "PigMonitorEcrStack", { env });

new PigMonitorStack(app, "PigMonitorStack", { env });
