import boto3
import os
from math import ceil
from time import sleep, time
from logs.log import logger
from kubernetes import client, config


class SQSPoller:

    options = None
    sqs_client = None
    apps_v1 = None
    last_message_count = None
    _deployment = None

    def __init__(self, options):
        self.options = options
        self.sqs_client = boto3.client('sqs', region_name=options.aws_region)
        config.load_incluster_config()

        if not self.options.sqs_queue_url:
            # derive the URL from the queue name
            self.options.sqs_queue_url = self.sqs_client.get_queue_url(
                QueueName=self.options.sqs_queue_name)['QueueUrl']

        self.apps_v1 = client.AppsV1Api()
        self.last_scale_up_time = time()
        self.last_scale_down_time = time()

    def message_count(self):
        response = self.sqs_client.get_queue_attributes(
            QueueUrl=self.options.sqs_queue_url,
            AttributeNames=['ApproximateNumberOfMessages']
        )
        return int(response['Attributes']['ApproximateNumberOfMessages'])

    @property
    def replicas(self):
        return self.deployment.spec.replicas

    def poll(self):
        """ Main polling function

            Scaling should work in the following way:
                a) If zero, assume it's intentional and do nothing
                b) Average messages per pod is used to decide scaling
                c) Scaling up/down should occur based on their own cooldowns
                d) Sleep at the end of every loop
        """
        if self.replicas == 0:
            # We don't want to try to scale up if intentionally
            # scaled to zero
            sleep(self.options.poll_period)
            return

        message_count = self.message_count()
        average_messages = int(message_count / self.replicas)
        t = time()
        if average_messages >= self.options.scale_up_messages:
            if t - self.last_scale_up_time > self.options.scale_up_cool_down:
                pods = int(ceil(message_count / self.options.scale_up_messages))
                self.scale_up(pods)
                self.last_scale_up_time = t
            else:
                logger.debug("Waiting for scale up cooldown")
        if average_messages <= self.options.scale_down_messages:
            if t - self.last_scale_down_time > self.options.scale_down_cool_down:
                pods = int(ceil(message_count / self.options.scale_down_messages))
                self.scale_down(pods)
                self.last_scale_down_time = t
            else:
                logger.debug("Waiting for scale down cooldown")

        # code for scale to use msg_count
        sleep(self.options.poll_period)

    def scale_up(self, pods):
        """ Perform scale up

            Pods should never go above max pods and we shouldn't try
            to scale if we're already at the desired pod count
        Args:
            pods: int, desired pod count
        """
        pods = pods if pods <= self.options.max_pods else self.options.max_pods
        if self.replicas != pods:
            logger.info(f"Scaling up to {pods}")
            self.deployment.spec.replicas = pods
            self.update_deployment()
        else:
            logger.info("Max pods reached")

    def scale_down(self, pods):
        """ Perform scale down

            Pods should never go below min pods and we shouldn't try
            to scale if we're already at the desired pod count
        Args:
            pods: int, desired pod count
        """
        pods = pods if pods >= self.options.min_pods else self.options.min_pods
        if self.replicas != pods:
            logger.info(f"Scaling down to {pods}")
            self.deployment.spec.replicas = pods
            self.update_deployment()
        else:
            logger.info("Min pods reached")

    @property
    def deployment(self):
        if self._deployment:
            return self._deployment

        logger.debug("loading deployment: {} from namespace: {}".format(
            self.options.kubernetes_deployment, self.options.kubernetes_namespace))
        deployments = self.apps_v1.list_namespaced_deployment(
            self.options.kubernetes_namespace, label_selector="app={}".format(self.options.kubernetes_deployment))
        self._deployment = deployments.items[0]
        return self._deployment

    def update_deployment(self):
        """ Update the deployment """
        api_response = self.apps_v1.patch_namespaced_deployment(
            name=self.options.kubernetes_deployment,
            namespace=self.options.kubernetes_namespace,
            body=self.deployment)
        logger.debug("Deployment updated. status='%s'" % str(api_response.status))

    def run(self):
        """ Run the poller forever """
        options = self.options
        logger.debug("Starting poll for {} every {}s".format(
            options.sqs_queue_url, options.poll_period))
        while True:
            self.poll()


def run(options):
    """
    poll_period is set as as part of k8s deployment env variable
    sqs_queue_url is set as as part of k8s deployment env variable
    """
    SQSPoller(options).run()
