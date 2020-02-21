#!/usr/bin/env python
import ekf_class
import numpy as np
import rospkg
from pyquaternion import Quaternion
import rospy
import tf

from geometry_msgs.msg import Pose, PoseArray, PoseStamped,TwistStamped
from apriltag_ros.msg import AprilTagDetectionArray
from apriltag_ros.msg import AprilTagDetection
from visualization_msgs.msg import Marker, MarkerArray
from sensor_msgs.msg import Imu
from mavros_msgs.srv import SetMode
from numpy import genfromtxt
import os

# NUM_P = 100
state_dim = 3  # x, y, z
# x_range = (0, 3)
# y_range = (0, 2)
# z_range = (0, 1.5)
# cov_mat = 1.5
# cov_mat = 0.05
roll_current = 0
pitch_current = 0
yaw_current = 0
cov_mat = 0.05
old_yaw = 0
# set_mode_srv = rospy.ServiceProxy('mavros/set_mode', SetMode)
# res = set_mode_srv(0, " OFFBOARD")

rospack = rospkg.RosPack()
#data_path = rospack.get_path("mu_auv_localization")+'/scripts/calibration_ground_truth_gazebo.csv' # in gazebo
data_path = rospack.get_path("mu_auv_localization") + '/scripts/calibration_tank.csv'  # in real tank
tags = genfromtxt(data_path, delimiter=',')  # home PC

tags = tags[:, 0:4]
print(tags)
tags[:, 3] += 0.0
# tags[:, 1] += 0.08  # to shift x-value according to gantry origin
# tags[:,2] += 0.02  # to shift y-value according to gantry origin
# print(tags)
rviz = False


def yaw_pitch_roll_to_quat(yaw, pitch, roll):
    cy = np.cos(yaw * 0.5)
    sy = np.sin(yaw * 0.5)
    cp = np.cos(pitch * 0.5)
    sp = np.sin(pitch * 0.5)
    cr = np.cos(roll * 0.5)
    sr = np.sin(roll * 0.5)
    return (Quaternion(x=cy * cp * sr - sy * sp * cr, y=sy * cp * sr + cy * sp * cr, z=sy * cp * cr - cy * sp * sr,
                       w=cy * cp * cr + sy * sp * sr))


def callback_imu(msg, tmp_list):
    global roll_current, pitch_current, yaw_current,old_yaw
    [ekf, publisher_position, publisher_mavros, broadcaster,
     publisher_marker,publisher_twist] = tmp_list

    tmp = yaw_pitch_roll_to_quat(yaw_current, pitch_current, roll_current).rotate(
        np.asarray([[-msg.linear_acceleration.x], [msg.linear_acceleration.y], [msg.linear_acceleration.z]]))
    #ekf.prediction(tmp[0], tmp[1], tmp[2] - 9.81)
    ekf.prediction(0, 0, 0)

    estimated_orientation = yaw_pitch_roll_to_quat(-(old_yaw - np.pi / 2), 0, 0)
    estimated_position = ekf.get_x_est()

    # [mm]
    x_mean_ned = estimated_position[0] * 1000  # global Tank Koordinate System(NED)
    y_mean_ned = estimated_position[1] * 1000
    z_mean_ned = estimated_position[2] * 1000

    # publish estimated_pose [m] in mavros to /mavros/vision_pose/pose
    # this pose needs to be in ENU
    mavros_position = PoseStamped()
    mavros_position.header.stamp = rospy.Time.now()
    mavros_position.header.frame_id = "map"
    mavros_position.pose.position.x = y_mean_ned / 1000  # NED Coordinate to ENU(ROS)
    mavros_position.pose.position.y = x_mean_ned / 1000
    mavros_position.pose.position.z = - z_mean_ned / 1000

    mavros_position.pose.orientation.w = estimated_orientation.w
    mavros_position.pose.orientation.x = estimated_orientation.x
    mavros_position.pose.orientation.y = estimated_orientation.y
    mavros_position.pose.orientation.z = estimated_orientation.z
    publisher_mavros.publish(mavros_position)  # oublish to boat

    # publish estimated_pose [m]
    position = PoseStamped()
    position.header.stamp = rospy.Time.now()
    position.header.frame_id = "global_tank"  # ned
    position.pose.position.x = x_mean_ned / 1000
    position.pose.position.y = y_mean_ned / 1000
    position.pose.position.z = z_mean_ned / 1000
    estimated_orientation = yaw_pitch_roll_to_quat(old_yaw, 0, 0)
    position.pose.orientation.w = estimated_orientation.w
    position.pose.orientation.x = estimated_orientation.x
    position.pose.orientation.y = estimated_orientation.y
    position.pose.orientation.z = estimated_orientation.z
    #publisher_position.publish(position)


    msg_twist=TwistStamped()
    msg_twist.header.stamp = rospy.Time.now()
    msg_twist.header.frame_id = "global_tank"  # ned
    msg_twist.twist.linear.x = estimated_position[3]
    msg_twist.twist.linear.y = estimated_position[4]
    msg_twist.twist.linear.z = estimated_position[5]
    publisher_twist.publish(msg_twist)


def callback_orientation(msg):
    global yaw_current, pitch_current, roll_current
    rotation_body_frame = Quaternion(w=msg.pose.orientation.w,
                                     x=msg.pose.orientation.x,
                                     y=msg.pose.orientation.y,
                                     z=msg.pose.orientation.z)
    yaw, pitch, roll = rotation_body_frame.inverse.yaw_pitch_roll
    yaw_current = -yaw
    pitch_current = -pitch
    roll_current = -((roll + 360 / 180.0 * np.pi) % (np.pi * 2) - 180 / 180.0 * np.pi)

    # yaw_current = yaw
    # pitch_current = pitch
    # roll_current = roll


def callback(msg, tmp_list):
    """"""
    global old_yaw
    [ekf, publisher_position, publisher_mavros, broadcaster,
     publisher_marker,publisher_twist] = tmp_list

    # ekf algorithm
    # ekf.prediction()

    # get length of message
    num_meas = len(msg.detections)
    orientation_yaw_pitch_roll = np.zeros((num_meas, 3))

    # if new measurement: update particles
    if num_meas >= 1:
        measurements = np.zeros((num_meas, 1 + state_dim))
        for i, tag in enumerate(msg.detections):
            tag_id = int(tag.id[0])
            tag_distance_cam = np.array(([tag.pose.pose.pose.position.x * 1.05,
                                          tag.pose.pose.pose.position.y * 1.1,
                                          tag.pose.pose.pose.position.z]))
            measurements[i, 0] = np.linalg.norm(tag_distance_cam)
            tmpquat = Quaternion(w=tag.pose.pose.pose.orientation.w,
                                 x=tag.pose.pose.pose.orientation.x,
                                 y=tag.pose.pose.pose.orientation.y,
                                 z=tag.pose.pose.pose.orientation.z)

            orientation_yaw_pitch_roll[i, :] = tmpquat.inverse.yaw_pitch_roll
            index = np.where(tags[:, 0] == tag_id)

            measurements[i, 1:4] = tags[index, 1:4]
        # ekf update step
        ekf.update(measurements)

        yaw_list = np.asarray(orientation_yaw_pitch_roll[:, 0])
        yaw = np.arctan2(np.mean(np.sin(yaw_list)), np.mean(np.cos(yaw_list)))
        pitch = np.mean(orientation_yaw_pitch_roll[:, 1])
        roll = np.mean(orientation_yaw_pitch_roll[:, 2])
    else:
        ekf.update(np.zeros((num_meas, 1 + state_dim)))
        yaw = old_yaw
    old_yaw = yaw
    # print "reale messungen: " + str(measurements)
    print("Angle yaw: " + str(np.round(yaw * 180 / np.pi, decimals=2)) + ", x_est = " + str(
        ekf.get_x_est().transpose()))


def main():
    rospy.init_node('ekf_node')

    ekf = ekf_class.ExtendedKalmanFilter()

    publisher_position = rospy.Publisher('estimated_pose', PoseStamped, queue_size=1)
    publisher_twist = rospy.Publisher('estimated_twist', TwistStamped, queue_size=1)
    publisher_mavros = rospy.Publisher('/mavros/vision_pose/pose', PoseStamped, queue_size=1)
    # publisher_particles = rospy.Publisher('particle_poses', PoseArray, queue_size=1)
    publisher_marker = rospy.Publisher('Sphere', MarkerArray, queue_size=1)
    broadcaster = tf.TransformBroadcaster()

    rospy.Subscriber("/tag_detections", AprilTagDetectionArray, callback,
                     [ekf, publisher_position, publisher_mavros, broadcaster,
                      publisher_marker,publisher_twist], queue_size=1)
    rospy.Subscriber("/mavros/imu/data", Imu, callback_imu,
                     [ekf, publisher_position, publisher_mavros, broadcaster,
                      publisher_marker,publisher_twist], queue_size=1)
    rospy.Subscriber("/mavros/local_position/pose_NED", PoseStamped, callback_orientation, queue_size=1)

    rospy.spin()


if __name__ == '__main__':
    main()
