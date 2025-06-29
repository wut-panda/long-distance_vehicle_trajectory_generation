import os
import numpy as np
from filterpy.kalman import KalmanFilter
import random
import matplotlib.pyplot as plt
import matplotlib
from draw_utils import draw_xy
from collections import Counter
import glob
import time
import argparse
from filterpy.kalman import KalmanFilter
import cv2 as cv

np.random.seed(0)

def linear_assignment(cost_matrix):
    try:
        import lap
        _, x, y = lap.lapjv(cost_matrix, extend_cost=True)
        return np.array([[y[i], i] for i in x if i >= 0])  #
    except ImportError:
        from scipy.optimize import linear_sum_assignment
        x, y = linear_sum_assignment(cost_matrix)
        return np.array(list(zip(x, y)))


def iou_batch(bb_test, bb_gt):
    """
    From SORT: Computes IUO between two bboxes in the form [l,t,w,h]
    """
    bb_gt = np.expand_dims(bb_gt, 0)
    bb_test = np.expand_dims(bb_test, 1)

    xx1 = np.maximum(bb_test[..., 0], bb_gt[..., 0])
    yy1 = np.maximum(bb_test[..., 1], bb_gt[..., 1])
    xx2 = np.minimum(bb_test[..., 2], bb_gt[..., 2])
    yy2 = np.minimum(bb_test[..., 3], bb_gt[..., 3])
    w = np.maximum(0., xx2 - xx1)
    h = np.maximum(0., yy2 - yy1)
    wh = w * h
    o = wh / ((bb_test[..., 2] - bb_test[..., 0]) * (bb_test[..., 3] - bb_test[..., 1])
              + (bb_gt[..., 2] - bb_gt[..., 0]) * (bb_gt[..., 3] - bb_gt[..., 1]) - wh)
    return (o)

def associate_detections_to_trackers(detections,trackers,iou_threshold = 0.3):
  """
  Assigns detections to tracked object (both represented as bounding boxes)

  Returns 3 lists of matches, unmatched_detections and unmatched_trackers
  """
  if(len(trackers)==0):
    return np.empty((0,2),dtype=int), np.arange(len(detections)), np.empty((0,5),dtype=int)

  iou_matrix = iou_batch(detections, trackers)

  if min(iou_matrix.shape) > 0:
    a = (iou_matrix > iou_threshold).astype(np.int32)
    if a.sum(1).max() == 1 and a.sum(0).max() == 1:
        matched_indices = np.stack(np.where(a), axis=1)
    else:
      matched_indices = linear_assignment(-iou_matrix)
  else:
    matched_indices = np.empty(shape=(0,2))

  unmatched_detections = []
  for d, det in enumerate(detections):
    if(d not in matched_indices[:,0]):
      unmatched_detections.append(d)
  unmatched_trackers = []
  for t, trk in enumerate(trackers):
    if(t not in matched_indices[:,1]):
      unmatched_trackers.append(t)

  #filter out matched with low IOU
  matches = []
  for m in matched_indices:
    if(iou_matrix[m[0], m[1]]<iou_threshold):
      unmatched_detections.append(m[0])
      unmatched_trackers.append(m[1])
    else:
      matches.append(m.reshape(1,2))
  if(len(matches)==0):
    matches = np.empty((0,2),dtype=int)
  else:
    matches = np.concatenate(matches,axis=0)

  return matches, np.array(unmatched_detections), np.array(unmatched_trackers)


class KalmanBoxTracker(object):
  """
  This class represents the internal state of individual tracked objects observed as bbox.
  """
  count = 0
  def __init__(self,bbox):
    """
    Initialises a tracker using initial bounding box.
    """
    #define constant velocity model
    self.kf = KalmanFilter(dim_x=7, dim_z=4)
    self.kf.F = np.array([[1,0,0,0,1,0,0],[0,1,0,0,0,1,0],[0,0,1,0,0,0,1],[0,0,0,1,0,0,0],  [0,0,0,0,1,0,0],[0,0,0,0,0,1,0],[0,0,0,0,0,0,1]])
    self.kf.H = np.array([[1,0,0,0,0,0,0],[0,1,0,0,0,0,0],[0,0,1,0,0,0,0],[0,0,0,1,0,0,0]])

    self.kf.R[2:,2:] *= 10.
    self.kf.P[4:,4:] *= 1000. #give high uncertainty to the unobservable initial velocities
    self.kf.P *= 10.
    self.kf.Q[-1,-1] *= 0.01
    self.kf.Q[4:,4:] *= 0.01

    self.kf.x[:4] = convert_bbox_to_z(bbox)
    self.time_since_update = 0
    self.id = KalmanBoxTracker.count
    KalmanBoxTracker.count += 1
    self.history = []
    self.hits = 0
    self.hit_streak = 0
    self.age = 0

  def update(self,bbox):
    """
    Updates the state vector with observed bbox.
    """
    self.time_since_update = 0
    self.history = []
    self.hits += 1
    self.hit_streak += 1
    self.kf.update(convert_bbox_to_z(bbox))

  def predict(self):
    """
    Advances the state vector and returns the predicted bounding box estimate.
    """
    if((self.kf.x[6]+self.kf.x[2])<=0):
      self.kf.x[6] *= 0.0
    self.kf.predict()
    self.age += 1
    if(self.time_since_update>0):
      self.hit_streak = 0
    # if (self.time_since_update > 1):  # 如果连续两帧都没有检测框的话，那么跟踪2帧
    #     self.hit_streak = 0
    self.time_since_update += 1
    self.history.append(convert_x_to_bbox(self.kf.x))
    return self.history[-1]

  def get_state(self):
    """
    Returns the current bounding box estimate.
    """
    return convert_x_to_bbox(self.kf.x)

def convert_bbox_to_z(bbox):
  """
  Takes a bounding box in the form [x1,y1,x2,y2] and returns z in the form
    [x,y,s,r] where x,y is the centre of the box and s is the scale/area and r is
    the aspect ratio
  """
  w = bbox[2] - bbox[0]
  h = bbox[3] - bbox[1]
  x = bbox[0] + w/2.
  y = bbox[1] + h/2.
  s = w * h    #scale is just area
  r = w / float(h)
  return np.array([x, y, s, r]).reshape((4, 1))


def convert_x_to_bbox(x,score=None):
  """
  Takes a bounding box in the centre form [x,y,s,r] and returns it in the form
    [x1,y1,x2,y2] where x1,y1 is the top left and x2,y2 is the bottom right
  """
  w = np.sqrt(x[2] * x[3])
  h = x[2] / w
  if(score==None):
    return np.array([x[0]-w/2.,x[1]-h/2.,x[0]+w/2.,x[1]+h/2.]).reshape((1,4))
  else:
    return np.array([x[0]-w/2.,x[1]-h/2.,x[0]+w/2.,x[1]+h/2.,score]).reshape((1,5))

if __name__=='__main__':
    txt_path='./outputs/output_test.txt'
    data = np.loadtxt(txt_path, delimiter=',', dtype=bytes).astype(str)
    new_data = data.astype(np.float64)
    # new_data = new_data[new_data[:,0]<=1000,:]

    area_X1,area_X2,area_Y1,area_Y2 = 3200, 3600, 900, 1300  ## 边框左上角坐标区域
    # area_X1,area_X2,area_Y1,area_Y2 = 2400, 2700, 900, 1300

#############################################################
#############################################################
    ## MOT
    max_age = 3
    min_hits = 3
    iou_threshold = 0.3
    frame_count = 0
    trackers = []
    big_trackers = []

    combine_data = np.empty((0, 11))
    for frame in range(int(new_data[:, 0].max())):
        if (frame+1) % 5 == 0:
            print(frame+1)

            frame_data=new_data[new_data[:, 0] == (frame + 1),:]
            frame_data[:,4:6]=frame_data[:, 4:6] + frame_data[:, 2:4]
            frame_data[:,6]=frame_data[:,10]
            frame_data[:,7] = frame_data[:,1]
            dets = frame_data[:,2:8]      ## 这里dets数据格式转为 [x1,y1,x2,y2,cls,id]
            dets_= dets.copy()
            dets_[:, 0]=dets_[:, 0]-0
            dets_[:, 1]=dets_[:, 1]-0
            dets_[:, 2]=dets_[:, 2]+0
            dets_[:, 3]=dets_[:, 3]+0

            #################
            frame_count += 1
            to_del = []
            ret = []
            ## 对trackers每一行进行预测，得到trks。trackers是一个kalman对象列表，仍是前一帧的数据，trks是预测过的边框矩阵。
            for trk in trackers:
                print(trk.get_state()[0])
            trks = np.zeros((len(trackers), 5))
            for t, trk in enumerate(trks):
                pos = trackers[t].predict()[0]
                trk[:] = [pos[0], pos[1], pos[2], pos[3], 0]
                if np.any(np.isnan(pos)):
                    to_del.append(t)
            ## 对trackers和trks删除无效值
            trks = np.ma.compress_rows(np.ma.masked_invalid(trks))  # 把arr中设置为mask的元素所在的行与列进行屏蔽
            for t in reversed(to_del):
                trackers.pop(t)
                big_trackers.pop(t)
            for trk in trackers:
                print(trk.get_state()[0])

            ###返回的是dets,trks的索引值
            matched, unmatched_dets, unmatched_trks = associate_detections_to_trackers(dets_, trks, iou_threshold)
            ## 利用匹配的dets更新trackers
            for m in matched:
                trackers[m[1]].update(dets_[m[0], :])
                trk_det = [trackers[m[1]], trackers[m[1]].id + 1, dets[m[0], :]]
                big_trackers[m[1]] = trk_det
            ## 将未匹配的dets创建新的trk，加入到trackers
            for i in unmatched_dets:
                trk = KalmanBoxTracker(dets_[i, :])
                trackers.append(trk)
                trk_det = [trk, trk.id + 1, dets[i, :]]
                big_trackers.append(trk_det)
            for trk in trackers:
                print(trk.get_state()[0])
            ## 这里当前帧trackers的数据基本上定下来了

            i = len(trackers)
            for trk_det in reversed(big_trackers):
                trk = trk_det[0]
                d = trk.get_state()[0]
                ## 连续预测的次数、 跟踪到目标的最小次数、 视频的一开始前N帧
                if (trk.time_since_update < 1) and (trk.hit_streak >= min_hits or frame_count <= min_hits):
                    ret.append(np.concatenate((trk_det[2][:], [trk_det[1]])).reshape(1, -1))
                i -= 1
                # 这里要再删除下trackers里的数据
                if (trk.time_since_update > max_age):
                    trackers.pop(i)
                    big_trackers.pop(i)
            if (len(ret) > 0):
                results = np.concatenate(ret)
            else:
                results = np.empty((0, 7))
            print(results)
            results = results.reshape((-1, 7))
            ##################################################

            temp_ = np.full((results.shape[0], 1), -1)
            frame_array = np.full((results.shape[0], 1), int(frame + 1))
            row_data = np.column_stack((frame_array, results[:, 5], results[:, 0], results[:, 1],
                                        results[:, 2] - results[:, 0], results[:, 3] - results[:, 1],
                                        results[:, 6],temp_, temp_, temp_,results[:, 4]))  ### 这里格式变为了[x1,y1,w,h] 为mot_det格式
        else:
            row_data=new_data[new_data[:, 0] == (frame + 1),:]
        combine_data = np.row_stack((combine_data, row_data))
    # print(combine_data.shape)
##################################################################
    new_combine_data=np.empty((0, combine_data.shape[1]))
    for i in range(int(combine_data[:, 1].max())):
        per_id_data = combine_data[combine_data[:, 1] == (i + 1), :]
        if per_id_data.shape[0] == 0:
            continue
        counter_value=Counter(per_id_data[:,6]).most_common()
        # print(counter_value)
        # print(counter_value[0])
        # print(counter_value[0][0])
        if counter_value[0][0]== -1:
            if len(counter_value) >1:
                per_id_data[:, 6] = counter_value[1][0]
        else:
            per_id_data[:,6]=counter_value[0][0]
        new_combine_data=np.row_stack((new_combine_data,per_id_data))
    # print(new_combine_data.shape)
####################################################################
####################################################################
    temp_data=new_combine_data.copy()
    temp_data1=temp_data[temp_data[:,2]>=area_X1,:]
    temp_data1=temp_data1[temp_data1[:,2]<=area_X2,:]
    temp_data1=temp_data1[temp_data1[:,3]>=area_Y1,:]
    temp_data1=temp_data1[temp_data1[:,3]<=area_Y2,:]
    # temp_data2=temp_data[temp_data[:,2]>=area_X1_2,:]
    # temp_data2=temp_data2[temp_data2[:,2]<=area_X2_2,:]
    # temp_data2=temp_data2[temp_data2[:,3]>=area_Y1_2,:]
    # temp_data2=temp_data2[temp_data2[:,3]<=area_Y2_2,:]
    # temp_data_=np.concatenate([temp_data1],axis=0)
    temp_data_=temp_data1
    print(temp_data_.shape)

    record_data = np.empty((0, 2))
    for i in range(int(temp_data_[:, 6].max())):
        per_id_data = temp_data_[temp_data_[:, 6] == (i + 1), :]
        if per_id_data.shape[0] == 0:
            continue
        res = set(per_id_data[:, 1].flatten().tolist())
        res = list(res)
        temp_record_data = np.zeros((len(res), 2))
        for t in range(len(res)):
            temp_record_data[t,0]=res[0]
            temp_record_data[t,1]=res[t]
        record_data=np.row_stack((record_data,temp_record_data))
    print(record_data)

    final_data=np.empty((0, new_combine_data.shape[1]))
    for i in range(int(new_combine_data[:, 1].max())):
        per_id_data = new_combine_data[new_combine_data[:, 1] == (i + 1), :]
        if per_id_data.shape[0] == 0:
            continue
        for j, value in enumerate(record_data[:,1]):
            if (i+1) == value:
                # temp_array=np.full((per_id_data[:,1].shape),record_data[j,0])
                per_id_data[:,1] = record_data[j,0]
        final_data=np.row_stack((final_data,per_id_data))
    final_data[:,6]=-1
#########################################################
    output_path='./outputs/output_downsampling.txt'
    np.savetxt(output_path, final_data,
               fmt=['%0.0f', '%0.0f', '%0.4f', '%0.4f', '%0.4f', '%0.4f',
                    '%0.0f', '%0.0f', '%0.0f','%0.0f','%0.0f'],delimiter=',')
